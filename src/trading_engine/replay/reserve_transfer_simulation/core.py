"""Controller puramente offline: hechos → particiones → confirmaciones → decisiones."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from hashlib import sha256
from uuid import UUID

from trading_engine.models._validation import finite_decimal, require_type
from trading_engine.models.ids import RoundId
from trading_engine.models.orders import Order, OrderCancelRequest
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.isolated_margin_risk.core import IsolatedMarginRisk
from trading_engine.replay.isolated_margin_risk.evidence import MarginAdjustmentEvidence, RiskStamp
from trading_engine.replay.isolated_margin_risk.models import (
    IsolatedPositionRisk,
    RiskObservation,
    RiskState,
)
from trading_engine.replay.reserve_transfer_simulation import accounting
from trading_engine.replay.reserve_transfer_simulation.evidence import (
    CapitalPartitionEvidence,
    FixtureReserveSource,
    OrderPriceEvidence,
)
from trading_engine.replay.reserve_transfer_simulation.models import (
    CapitalPartition,
    ReserveAudit,
    ReserveDecision,
    ReserveReport,
    ReserveState,
    ScheduledTransfer,
    TransferRequest,
)
from trading_engine.replay.reserve_transfer_simulation.orders import (
    money,
    receive_cancel,
    receive_order,
    reservations,
)
from trading_engine.replay.reserve_transfer_simulation.policy import (
    AutoMarginPolicy,
    ReservationPolicy,
)
from trading_engine.utils.time import require_utc

ZERO = Decimal(0)
PENDING = {"FUNDS_HELD", "REQUEST_PENDING", "CONFIRMATION_PENDING"}
TERMINAL = {"CONFIRMED_SIMULATED", "REJECTED", "EXPIRED"}


@dataclass(frozen=True)
class ReserveTransferSimulation:
    reservation_policy: ReservationPolicy
    auto_policy: AutoMarginPolicy
    capital: CapitalPartitionEvidence | None
    source: FixtureReserveSource = FixtureReserveSource()
    initial_orders: tuple[Order, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.reservation_policy, ReservationPolicy, "reservation_policy")
        require_type(self.auto_policy, AutoMarginPolicy, "auto_policy")
        require_type(self.source, FixtureReserveSource, "source")
        require_type(self.initial_orders, tuple, "initial_orders")
        if self.capital is not None:
            require_type(self.capital, CapitalPartitionEvidence, "capital")
        for order in self.initial_orders:
            require_type(order, Order, "initial_order")
            if order.symbol != Symbol("1000SHIBUSDC"):
                raise ValueError("Orden inicial de otro símbolo")
        if len({o.order_id for o in self.initial_orders}) != len(self.initial_orders):
            raise ValueError("Órdenes iniciales duplicadas")

    @property
    def policy_identity(self) -> str:
        return (
            "offline-reserve-transfers-v1:"
            + sha256(
                encoded(
                    (
                        self.reservation_policy,
                        self.auto_policy,
                        self.capital,
                        self.initial_orders,
                    )
                )
            ).hexdigest()
        )

    @property
    def identity(self) -> str:
        return (
            "reserve-session-v1:"
            + sha256(encoded((self.policy_identity, self.source.identity))).hexdigest()
        )

    def initial(self) -> ReserveState:
        # Las partidas desconocidas nunca se completan por inferencia.
        missing = money(None, "NOT_INITIALIZED")
        state = ReserveState(
            self.identity,
            self.capital,
            CapitalPartition(
                missing, missing, missing, missing, missing, ZERO, "PARTITION_NOT_RECONCILED"
            ),
            orders=self.initial_orders,
        )
        partition = accounting.declared_partition(state)
        return replace(state, settled_partition=partition)

    def audit(
        self,
        state: ReserveState,
        at: datetime,
        kind: str,
        payload: object,
        round_id: RoundId | None = None,
    ) -> ReserveState:
        require_utc(at)
        if state.identity != self.identity or state.last_at is not None and at < state.last_at:
            raise ValueError("Estado incompatible o reloj retrospectivo")
        sequence = len(state.journal)
        event_id = sha256(encoded((self.policy_identity, at, sequence, kind, payload))).hexdigest()
        stamp = RiskStamp(
            event_id,
            at,
            at,
            sequence,
            self.reservation_policy.source_reference,
            "reserve-transfer-50-v1",
        )
        # RoundId verificado por los contratos de entrada.
        if round_id is not None and not isinstance(round_id, UUID):
            raise TypeError("round_id inválido")
        rid = None if round_id is None else RoundId(round_id)
        return replace(
            state, journal=state.journal + (ReserveAudit(stamp, kind, rid, payload),), last_at=at
        )

    def order(self, state: ReserveState, order: Order, at: datetime) -> ReserveState:
        updated = receive_order(state, order, at)
        return (
            state
            if updated == state
            else self.audit(updated, at, "ORDER_RECEIVED", order, order.round_id)
        )

    def cancel(
        self, state: ReserveState, request: OrderCancelRequest, at: datetime
    ) -> ReserveState:
        updated = receive_cancel(state, request, at)
        return (
            state
            if updated == state
            else self.audit(updated, at, "CANCEL_REQUESTED", request, request.intent.round_id)
        )

    @staticmethod
    def next_at(state: ReserveState) -> datetime | None:
        return state.queue[0].scheduled_at if state.queue else None

    def _decision(
        self,
        state: ReserveState,
        at: datetime,
        position: IsolatedPositionRisk,
        status: str,
        reason: str,
        amount: Decimal | None,
        request_id: str | None = None,
    ) -> ReserveState:
        item = ReserveDecision(at, position.round_id, status, reason, amount, request_id)
        return self.audit(
            replace(state, decisions=state.decisions + (item,)),
            at,
            "AUTO_MARGIN_DECISION",
            item,
            position.round_id,
        )

    def _eligibility(
        self,
        state: ReserveState,
        position: IsolatedPositionRisk,
        amount: Decimal,
        risk: IsolatedMarginRisk,
    ) -> str | None:
        if state.conflicts or state.partition.status != "RECONCILED":
            return "ALLOCATION_UNKNOWN"
        if (
            risk.policy.initial != "EXECUTED_NOTIONAL_OVER_LEVERAGE"
            or risk.policy.costs != "RECONCILED_TO_ISOLATED"
            or position.open_quantity <= 0
            or position.isolated_wallet_balance.value is None
            or position.buffer.value is None
            or position.mark_id is None
            or position.schedule_id is None
        ):
            return "INSUFFICIENT_EVIDENCE"
        if any(r.round_id == position.round_id and r.state in PENDING for r in state.transfers):
            return "TRANSFER_NOT_ALLOWED:PENDING_REQUEST"
        if (
            sum(r.state in PENDING for r in state.transfers)
            >= self.auto_policy.max_simultaneous_requests
        ):
            return "TRANSFER_LIMIT_EXCEEDED:SIMULTANEOUS"
        used = sum(
            (
                r.amount_usdc
                for r in state.transfers
                if r.round_id == position.round_id and r.state not in {"REJECTED", "EXPIRED"}
            ),
            ZERO,
        )
        if (
            amount < self.auto_policy.minimum_transfer_usdc
            or amount > self.auto_policy.maximum_transfer_usdc
            or amount + used > self.auto_policy.max_per_round_usdc
        ):
            return "TRANSFER_LIMIT_EXCEEDED"
        if state.partition.free_balance.value is None:
            return "ALLOCATION_UNKNOWN"
        if amount > state.partition.free_balance.value:
            return "INSUFFICIENT_FREE_BALANCE"
        # NO_RETRY: no nueva solicitud económica para una ronda tras rechazo/vencimiento.
        if any(
            r.round_id == position.round_id and r.state in {"REJECTED", "EXPIRED"}
            for r in state.transfers
        ):
            return "TRANSFER_NOT_ALLOWED:NO_RETRY"
        return None

    def propose(
        self,
        state: ReserveState,
        at: datetime,
        position: IsolatedPositionRisk,
        amount: Decimal,
        request_id: str,
        risk: IsolatedMarginRisk,
    ) -> ReserveState:
        require_utc(at)
        finite_decimal(amount, "transfer amount", nonnegative=True)
        if amount <= 0:
            raise ValueError("Transferencia requiere importe positivo")
        if state.last_at is not None and at < state.last_at:
            raise ValueError("Propuesta retrospectiva")
        with localcontext(ledger_context()):
            return self._propose(state, at, position, amount, request_id, risk)

    def _propose(
        self,
        state: ReserveState,
        at: datetime,
        position: IsolatedPositionRisk,
        amount: Decimal,
        request_id: str,
        risk: IsolatedMarginRisk,
    ) -> ReserveState:
        previous = next((r for r in state.transfers if r.request_id == request_id), None)
        if previous is not None:
            if previous.amount_usdc != amount or previous.round_id != position.round_id:
                raise ValueError("Transferencia duplicada contradictoria")
            return state
        reason = self._eligibility(state, position, amount, risk)
        if state.risk is None or state.risk.at != at or position not in state.risk.positions:
            reason = "TRANSFER_NOT_ALLOWED:NONCAUSAL_POSITION"
        p = self.auto_policy
        try:
            request_at = at + timedelta(milliseconds=p.request_latency_ms)
            confirmation_at = request_at + timedelta(milliseconds=p.confirmation_latency_ms)
            expires_at = at + timedelta(milliseconds=p.timeout_ms)
        except OverflowError as exc:
            raise ValueError("Latencia fuera del rango del reloj") from exc
        request = TransferRequest(
            request_id,
            "hold:" + request_id,
            position.round_id,
            amount,
            ZERO,
            ZERO,
            "PROPOSED",
            at,
            request_at,
            confirmation_at,
            expires_at,
            0,
            position.buffer.value,
            state.partition,
        )
        if position.buffer.value is not None and position.buffer.value <= 0:
            request = replace(
                request, first_breach_at=at, first_breach_buffer=position.buffer.value
            )
        state = self.audit(
            replace(state, transfers=state.transfers + (request,)),
            at,
            "PROPOSED",
            request,
            request.round_id,
        )
        if reason is not None:
            terminal = replace(
                request,
                state="REJECTED",
                terminal_at=at,
                rejection_reason=reason,
                balance_after=state.partition,
            )
            state = self._replace_request(state, terminal)
            state = self.audit(state, at, "REJECTED", terminal, request.round_id)
            return self._decision(state, at, position, "REJECTED", reason, amount, request_id)
        state = accounting.hold_transfer(state, amount)
        held = replace(request, held_usdc=amount, state="FUNDS_HELD")
        state = self.audit(
            self._replace_request(state, held), at, "FUNDS_HELD", held, request.round_id
        )
        waiting = replace(held, state="REQUEST_PENDING")
        state = self.audit(
            self._replace_request(state, waiting), at, "REQUEST_PENDING", waiting, request.round_id
        )
        seq = len(state.journal)
        queue = state.queue + (
            ScheduledTransfer(request_at, seq, request_id, "SENT"),
            ScheduledTransfer(confirmation_at, seq + 1, request_id, "CONFIRM"),
            ScheduledTransfer(expires_at, seq + 2, request_id, "EXPIRE"),
        )
        state = replace(state, queue=tuple(sorted(queue, key=lambda x: x.key)))
        return self._decision(
            state, at, position, "AUTHORIZED_SIMULATED", "FULL_AMOUNT_ONLY", amount, request_id
        )

    @staticmethod
    def _replace_request(state: ReserveState, request: TransferRequest) -> ReserveState:
        return replace(
            state,
            transfers=tuple(
                request if r.request_id == request.request_id else r for r in state.transfers
            ),
        )

    def terminal(
        self,
        state: ReserveState,
        risk: IsolatedMarginRisk,
        risk_state: RiskState,
        at: datetime,
        request_id: str,
        kind: str,
        amount: Decimal | None = None,
        reason: str | None = None,
    ) -> tuple[ReserveState, RiskState]:
        require_utc(at)
        if state.last_at is not None and at < state.last_at:
            raise ValueError("Transición retrospectiva")
        request = next((r for r in state.transfers if r.request_id == request_id), None)
        if request is None or request.state in TERMINAL:
            raise ValueError("Solicitud ausente o terminal; transición duplicada")
        if kind not in {"CONFIRM", "REJECT", "EXPIRE"}:
            raise ValueError("Transición terminal desconocida")
        if kind == "EXPIRE" and at < request.expires_at:
            raise ValueError("Expiración anticipada")
        if kind == "CONFIRM":
            if risk_state.context is None or risk_state.context.at != at:
                raise ValueError("Confirmación requiere contexto financiero actualizado")
            if request.state != "CONFIRMATION_PENDING" or at < request.scheduled_confirmation_at:
                raise ValueError("Confirmación fuera de orden/antes de latencia")
            if at >= request.expires_at:
                raise ValueError("Confirmación fuera del timeout")
            if amount != request.held_usdc or amount != request.amount_usdc:
                raise ValueError("Confirmación exige importe completo y hold exacto")
            position = next(
                (
                    p
                    for p in risk.report(risk_state, at).current.positions
                    if p.round_id == request.round_id
                ),
                None,
            )
            if position is None or position.open_quantity <= 0:
                return self.terminal(
                    state,
                    risk,
                    risk_state,
                    at,
                    request_id,
                    "REJECT",
                    reason="ROUND_CLOSED_WITH_PENDING_TRANSFER",
                )
            if state.partition.status != "RECONCILED":
                return self.terminal(
                    state,
                    risk,
                    risk_state,
                    at,
                    request_id,
                    "REJECT",
                    reason="ALLOCATION_UNKNOWN_AT_CONFIRMATION",
                )
            assert amount is not None
            stamp = RiskStamp(
                "confirmation:" + request_id,
                at,
                at,
                len(state.adjustments),
                self.auto_policy.source_reference,
                "reserve-transfer-50-v1",
            )
            evidence = MarginAdjustmentEvidence(
                stamp,
                request.round_id,
                amount,
                "ADD",
                request_id=request_id,
                hold_id=request.hold_id,
            )
            updated_risk = risk.observe(risk_state, RiskObservation(at, evidence))
            if updated_risk.conflicts:
                raise ValueError(
                    "Ajuste generado rechazado por contrato de riesgo:"
                    + updated_risk.conflicts[0].reason
                )
            state = accounting.release_transfer(state, amount, confirmed=True)
            request = replace(
                request,
                state="CONFIRMED_SIMULATED",
                held_usdc=ZERO,
                confirmed_usdc=amount,
                confirmed_at=at,
                terminal_at=at,
                confirmation=evidence,
                risk_outcome="SAMPLED_BREACH_BEFORE_CONFIRMATION"
                if request.first_breach_at is not None
                else "CONFIRMED_BEFORE_SAMPLED_BREACH",
            )
            state = replace(state, adjustments=state.adjustments + (evidence,))
            risk_state = updated_risk
        else:
            state = accounting.release_transfer(state, request.held_usdc, confirmed=False)
            request = replace(
                request,
                state="REJECTED" if kind == "REJECT" else "EXPIRED",
                held_usdc=ZERO,
                terminal_at=at,
                rejection_reason=reason or kind,
            )
        request = replace(request, balance_after=state.partition)
        state = self._replace_request(state, request)
        state = replace(state, queue=tuple(q for q in state.queue if q.request_id != request_id))
        state = self.audit(state, at, request.state, request, request.round_id)
        return state, risk_state

    def tick(
        self, state: ReserveState, risk: IsolatedMarginRisk, risk_state: RiskState, at: datetime
    ) -> tuple[ReserveState, RiskState]:
        require_utc(at)
        if state.identity != self.identity or state.last_at is not None and at < state.last_at:
            raise ValueError("Controller incompatible o retrospectivo")
        if risk_state.context is None or risk_state.context.at != at or risk_state.conflicts:
            raise ValueError("Controller requiere contexto causal no bloqueado")
        try:
            with localcontext(ledger_context()):
                return self._tick(state, risk, risk_state, at)
        except DecimalException as exc:
            raise ValueError("Importe de reserva no representable") from exc

    def _tick(
        self, state: ReserveState, risk: IsolatedMarginRisk, risk_state: RiskState, at: datetime
    ) -> tuple[ReserveState, RiskState]:
        context = risk_state.context
        assert context is not None
        if not state.initialized and not any(
            e.kind == "POLICY_AND_CAPITAL_EVIDENCE" for e in state.journal
        ):
            state = self.audit(
                state,
                at,
                "POLICY_AND_CAPITAL_EVIDENCE",
                (
                    self.reservation_policy,
                    self.auto_policy,
                    self.capital,
                    self.initial_orders,
                ),
            )
        # Evidencia confirmada del ledger puede aportar la orden terminal faltante.
        for entry in context.ledger.entries:
            known = next((o for o in state.orders if o.order_id == entry.order_id), None)
            if known is None or entry.evidence.order.updated_at > known.updated_at:
                state = self.order(state, entry.evidence.order, at)
        events = tuple(self.source.read(state.source_cursor, at))
        proposals = []
        commands = []
        for event in events:
            if event.stamp.received_at != at:
                raise ValueError("Evento de reserva fuera de recepción causal")
            previous = next(
                (e for e in state.received_events if e.stamp.event_id == event.stamp.event_id), None
            )
            if previous is not None:
                comparable = replace(
                    event,
                    stamp=replace(
                        event.stamp,
                        received_at=previous.stamp.received_at,
                        sequence=previous.stamp.sequence,
                    ),
                )
                if comparable != previous:
                    raise ValueError("Identidad de reserva contradictoria")
                state = replace(state, source_cursor=state.source_cursor + 1)
                continue
            state = replace(
                state,
                source_cursor=state.source_cursor + 1,
                received_events=state.received_events + (event,),
                processed_event_ids=state.processed_event_ids + (event.stamp.event_id,),
            )
            if isinstance(event, OrderPriceEvidence):
                state = replace(state, prices=state.prices + (event,))
            elif event.kind == "PROPOSE":
                proposals.append(event)
            else:
                commands.append(event)
            state = self.audit(state, at, "SOURCE_EVENT", event, getattr(event, "round_id", None))
        pre = risk.report(risk_state, at).current
        previous_partition, previous_reservations = state.partition, state.reservations
        state = replace(
            state,
            reservations=reservations(
                state, context, self.reservation_policy, at, risk.policy.leverage
            ),
        )
        state = accounting.reconcile(state, context, pre)
        if previous_partition != state.partition or previous_reservations != state.reservations:
            state = self.audit(
                state,
                at,
                "HOLDS_AND_PARTITIONS_RECONCILED",
                (
                    previous_partition,
                    state.partition,
                    previous_reservations,
                    state.reservations,
                    tuple(e.fill_id for e in context.ledger.entries),
                ),
            )
        state = replace(state, samples=state.samples + (pre,))
        state = self.audit(state, at, "PRE_CONFIRMATION_RISK", pre)
        for request in state.transfers:
            position = next((p for p in pre.positions if p.round_id == request.round_id), None)
            if (
                request.state in PENDING
                and position is not None
                and position.buffer.value is not None
                and position.buffer.value <= 0
                and request.first_breach_at is None
            ):
                breach = replace(
                    request, first_breach_at=at, first_breach_buffer=position.buffer.value
                )
                state = self.audit(
                    self._replace_request(state, breach),
                    at,
                    "SAMPLED_BREACH_BEFORE_CONFIRMATION",
                    breach,
                    breach.round_id,
                )
        # Terminales fixture preceden a la cola; EXPIRE gana un empate con CONFIRM.
        for command in commands:
            matched_request = next(
                (r for r in state.transfers if r.request_id == command.request_id), None
            )
            if matched_request is None or matched_request.round_id != command.round_id:
                raise ValueError("Acción de transferencia vinculada a otra ronda")
            state, risk_state = self.terminal(
                state,
                risk,
                risk_state,
                at,
                command.request_id,
                command.kind,
                command.amount_usdc,
                command.reason,
            )
        while state.queue and state.queue[0].scheduled_at <= at:
            scheduled = state.queue[0]
            if scheduled.scheduled_at != at:
                raise ValueError("Cola de transferencia atrasada")
            state = replace(state, queue=state.queue[1:])
            request = next(r for r in state.transfers if r.request_id == scheduled.request_id)
            if at >= request.expires_at:
                state, risk_state = self.terminal(
                    state, risk, risk_state, at, request.request_id, "EXPIRE", reason="TIMEOUT"
                )
            elif scheduled.kind == "SENT":
                if request.state != "REQUEST_PENDING":
                    raise ValueError("Envío duplicado/fuera de orden")
                sent = replace(request, state="CONFIRMATION_PENDING", attempts=1)
                state = self.audit(
                    self._replace_request(state, sent),
                    at,
                    "CONFIRMATION_PENDING",
                    sent,
                    sent.round_id,
                )
            elif scheduled.kind == "CONFIRM":
                state, risk_state = self.terminal(
                    state, risk, risk_state, at, request.request_id, "CONFIRM", request.held_usdc
                )
            else:
                raise ValueError("Evento programado desconocido")
        risk_state = risk.observe(risk_state, RiskObservation(at, "VALUE"))
        if risk_state.conflicts:
            raise ValueError("Evaluación 4A-4A bloqueada")
        final = risk_state.evaluations[-1]
        state = accounting.reconcile(state, context, final)
        for request in state.transfers:
            if request.confirmed_at == at:
                position = next(p for p in final.positions if p.round_id == request.round_id)
                state = self._replace_request(
                    state,
                    replace(
                        request, buffer_after=position.buffer.value, balance_after=state.partition
                    ),
                )
        state = replace(state, risk=final)
        state = self.audit(state, at, "POST_CONFIRMATION_RISK", final)
        for command in proposals:
            position = next((p for p in final.positions if p.round_id == command.round_id), None)
            if position is None:
                raise ValueError("Propuesta sin ronda confirmada")
            assert command.amount_usdc is not None
            state = self.propose(state, at, position, command.amount_usdc, command.request_id, risk)
        if self.auto_policy.enabled:
            for position in final.positions:
                if position.open_quantity <= 0:
                    continue
                if position.buffer.value is None or state.partition.status != "RECONCILED":
                    state = self._decision(
                        state, at, position, "NOT_AVAILABLE", "INSUFFICIENT_EVIDENCE", None
                    )
                elif position.buffer.value <= self.auto_policy.trigger_buffer_usdc:
                    if position.buffer.value <= 0:
                        state = self.audit(
                            state, at, "BREACH_ALREADY_OBSERVED", position.buffer, position.round_id
                        )
                    amount = max(ZERO, self.auto_policy.target_buffer_usdc - position.buffer.value)
                    if amount <= 0:
                        continue
                    reason = self._eligibility(state, position, amount, risk)
                    if reason is not None and reason != "INSUFFICIENT_FREE_BALANCE":
                        state = self._decision(
                            state,
                            at,
                            position,
                            "NOT_AVAILABLE"
                            if reason in {"ALLOCATION_UNKNOWN", "INSUFFICIENT_EVIDENCE"}
                            else "NOT_AUTHORIZED",
                            reason,
                            amount,
                        )
                    else:
                        request_id = sha256(
                            encoded(
                                (
                                    self.policy_identity,
                                    position.round_id,
                                    at,
                                    position.mark_id,
                                    len(state.transfers),
                                )
                            )
                        ).hexdigest()
                        state = self.propose(state, at, position, amount, request_id, risk)
        # Envío de latencia cero es causal; confirmación total siempre > 0.
        while state.queue and state.queue[0].scheduled_at == at:
            scheduled = state.queue[0]
            if scheduled.kind != "SENT":
                raise ValueError("Nueva confirmación no puede ser inmediata")
            request = next(r for r in state.transfers if r.request_id == scheduled.request_id)
            sent = replace(request, state="CONFIRMATION_PENDING", attempts=1)
            state = replace(state, queue=state.queue[1:])
            state = self.audit(
                self._replace_request(state, sent), at, "CONFIRMATION_PENDING", sent, sent.round_id
            )
        return state, risk_state

    def report(self, state: ReserveState) -> ReserveReport:
        breached = any(r.first_breach_at is not None for r in state.transfers)
        return ReserveReport(
            self.identity,
            state,
            trajectory="CONDITIONED_COUNTERFACTUAL_AFTER_SAMPLED_BREACH"
            if breached
            else "SAMPLED_COUNTERFACTUAL_ONLY",
        )
