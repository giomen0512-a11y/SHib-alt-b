"""Ledger puro de costo promedio móvil: hechos recibidos, nunca intenciones."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, DecimalException, localcontext

from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.enums import Side
from trading_engine.models.ids import RoundId
from trading_engine.replay.financial_ledger.models import (
    LedgerClosure,
    LedgerConflict,
    LedgerEntry,
    LedgerReport,
    LedgerState,
    RoundLedger,
)
from trading_engine.replay.financial_ledger.policy import (
    ExecutionOrigin,
    LedgerPolicy,
    ledger_context,
)
from trading_engine.replay.ports import FillEvidence, ReplayFact
from trading_engine.utils.time import require_utc


@dataclass(frozen=True)
class FinancialLedger:
    policy: LedgerPolicy = LedgerPolicy()

    def __post_init__(self) -> None:
        require_type(self.policy, LedgerPolicy, "policy")

    @property
    def identity(self) -> str:
        return self.policy.identity

    def initial(self) -> LedgerState:
        return LedgerState(self.policy, self.identity)

    def _time(self, state: LedgerState, at: datetime) -> None:
        require_type(state, LedgerState, "state")
        require_utc(at)
        if state.policy != self.policy or state.identity != self.identity:
            raise ValueError("Ledger de otra política/versión")
        if state.last_at is not None and at < state.last_at:
            raise ValueError("Ledger no permite recepción retrospectiva")

    def block(
        self,
        state: LedgerState,
        at: datetime,
        reason: str,
        observation: ReplayFact | LedgerClosure | None = None,
    ) -> LedgerState:
        self._time(state, at)
        nonblank(reason, "reason")
        if state.conflicts:
            return state
        rounds = tuple(
            replace(r, reconciliation_status="RECONCILIATION_REQUIRED") for r in state.round_ledger
        )
        return replace(
            state,
            round_ledger=rounds,
            conflicts=(LedgerConflict(at, reason, observation),),
            last_at=at,
        )

    def receive(self, state: LedgerState, fact: ReplayFact, *, at: datetime) -> LedgerState:
        self._time(state, at)
        require_type(fact, ReplayFact, "fact")
        if state.conflicts:
            return state
        try:
            if fact.received_at > at:
                return self.block(state, at, "Fill futuro no disponible")
            return self._receive(state, fact, at)
        except (ValueError, TypeError, DecimalException) as exc:
            return self.block(state, at, f"{type(exc).__name__}:{exc}", fact)

    def _receive(self, state: LedgerState, fact: ReplayFact, at: datetime) -> LedgerState:
        if fact.received_at != at:
            raise ValueError("Fill fuera de su instante de recepción")
        if not isinstance(fact.payload, FillEvidence):
            raise TypeError("Solo FillEvidence produce un asiento económico")
        evidence = fact.payload
        fill, order = evidence.fill, evidence.order
        intent = order.intent
        origin = ExecutionOrigin(fact.provenance)
        if origin not in self.policy.allowed_origins:
            raise ValueError("Procedencia de ejecución no permitida")
        if intent.symbol != self.policy.symbol:
            raise ValueError("Símbolo ajeno al contrato contable")
        if (intent.side is Side.BUY and intent.reduce_only) or (
            intent.side is Side.SELL and not intent.reduce_only
        ):
            raise ValueError("Side/reduce_only incompatible con contabilidad LONG")
        if not order.created_at <= fill.timestamp <= fact.received_at:
            raise ValueError("Fill no causal respecto a Order/recepción")
        if fill.order_id != order.order_id or order.updated_at > at:
            raise ValueError("OrderId/tiempo incompatible con Fill")

        def binding(value: FillEvidence) -> tuple[object, ...]:
            return value.order.intent, value.order.created_at, value.order.adapter_order_id

        prior = next((e for e in state.entries if e.fill_id == fill.fill_id), None)
        if prior is not None:
            if (
                prior.evidence.fill != fill
                or binding(prior.evidence) != binding(evidence)
                or prior.provenance != origin
            ):
                raise ValueError("FillId conflictivo; no sobrescribir asiento")
            return state  # Recepción repetida o nuevo snapshot acumulado no añade economía.
        for entry in state.entries:
            if entry.order_id == order.order_id or entry.intent_id == intent.intent_id:
                if entry.order_id != order.order_id or binding(entry.evidence) != binding(evidence):
                    raise ValueError("OrderId/IntentId vinculado a otro contenido")
        entries = tuple(e for e in state.entries if e.round_id == intent.round_id)
        if entries and (fill.timestamp, fact.sequence) <= (
            entries[-1].executed_at,
            entries[-1].sequence,
        ):
            raise ValueError("Secuencia económica tardía o empate sin orden explícito")
        current = next(
            (r for r in state.round_ledger if r.round_id == intent.round_id),
            RoundLedger(intent.round_id, intent.symbol),
        )
        if current.closed_at is not None:
            raise ValueError("Fill nuevo después de cierre financiero conciliado")
        with localcontext(ledger_context()):
            order_quantities = [
                (e.fill_id.int, e.quantity) for e in state.entries if e.order_id == order.order_id
            ] + [(fill.fill_id.int, fill.quantity.value)]
            per_order = sum((q for _, q in sorted(order_quantities)), Decimal("0"))
            if per_order > intent.quantity.value:
                raise ValueError("Fills exceden cantidad solicitada de Order")
            quantity, price = fill.quantity.value, fill.price.value
            notional = quantity * price
            if notional <= 0:
                raise ValueError("Nocional positivo no representable a precisión 50")
            # Cantidades canónicas por FillId/lado, compatibles con ejecución/estrategia.
            values = [(e.fill_id.int, e.side, e.quantity, e.notional_usdc) for e in entries]
            values.append((fill.fill_id.int, intent.side, quantity, notional))
            values.sort()

            def total(side: Side, *, quantity: bool) -> Decimal:
                return sum(
                    (v[2] if quantity else v[3] for v in values if v[1] is side), Decimal("0")
                )

            buy, sell = total(Side.BUY, quantity=True), total(Side.SELL, quantity=True)
            bought, sold = total(Side.BUY, quantity=False), total(Side.SELL, quantity=False)
            remaining = buy - sell
            if remaining < 0 or (intent.side is Side.SELL and quantity > current.open_quantity):
                raise ValueError("SELL supera exposición LONG confirmada")
            released = Decimal("0")
            cost = current.open_cost_basis_usdc
            pnl = Decimal("0")
            if intent.side is Side.BUY:
                cost += notional
                if current.sell_quantity == 0:
                    cost = bought  # Sin salidas, todo nocional BUY canónico permanece abierto.
            else:
                assert current.average_open_cost_usdc is not None
                released = quantity * current.average_open_cost_usdc
                # Venta final exacta consume todo el costo, sin tolerancias de cantidad.
                if remaining == 0:
                    released = cost
                cost -= released
                pnl = notional - released
            if cost < 0 or (remaining == 0 and cost != 0):
                raise ValueError("Costo remanente incompatible con exposición")
            average = cost / remaining if remaining > 0 else None
            if average is not None and average <= 0:
                raise ValueError("Costo promedio positivo no representable a precisión 50")
            realized = (
                current.gross_realized_pnl_usdc
                if intent.side is Side.BUY
                else sold - (bought - cost)
            )
        entry = LedgerEntry(
            fill.fill_id,
            order.order_id,
            intent.intent_id,
            intent.round_id,
            intent.symbol,
            intent.side,
            quantity,
            price,
            notional,
            fill.timestamp,
            at,
            fact.sequence,
            origin,
            released,
            pnl,
            remaining,
            cost,
            evidence,
        )
        updated = replace(
            current,
            buy_quantity=buy,
            sell_quantity=sell,
            gross_buy_notional_usdc=bought,
            gross_sell_notional_usdc=sold,
            open_quantity=remaining,
            open_cost_basis_usdc=cost,
            average_open_cost_usdc=average,
            gross_realized_pnl_usdc=realized,
        )
        rounds = tuple(r for r in state.round_ledger if r.round_id != intent.round_id) + (updated,)
        return replace(
            state,
            entries=state.entries + (entry,),
            round_ledger=tuple(sorted(rounds, key=lambda r: r.round_id.int)),
            journal=state.journal + (fact,),
            last_at=at,
        )

    def reconcile_closed(
        self, state: LedgerState, round_ids: tuple[RoundId, ...], *, at: datetime
    ) -> LedgerState:
        self._time(state, at)
        if state.conflicts:
            return state
        for round_id in sorted(round_ids, key=lambda r: r.int):
            closure = LedgerClosure(round_id, at)
            current = next((r for r in state.round_ledger if r.round_id == round_id), None)
            if current is None or current.open_quantity != 0 or current.open_cost_basis_usdc != 0:
                return self.block(
                    state, at, "CLOSED estratégico sin posición contable cero", closure
                )
            if current.closed_at is None:
                state = replace(
                    state,
                    round_ledger=tuple(
                        replace(r, closed_at=at) if r.round_id == round_id else r
                        for r in state.round_ledger
                    ),
                    journal=state.journal + (closure,),
                    last_at=at,
                )
        return state

    def validate(self, state: LedgerState, at: datetime) -> None:
        self._time(state, at)
        rebuilt = self.initial()
        for observation in state.journal:
            if isinstance(observation, ReplayFact):
                if observation.received_at > at:
                    raise ValueError("Asiento futuro en snapshot")
                rebuilt = self.receive(rebuilt, observation, at=observation.received_at)
            else:
                if observation.confirmed_at > at:
                    raise ValueError("Cierre futuro en snapshot")
                rebuilt = self.reconcile_closed(
                    rebuilt, (observation.round_id,), at=observation.confirmed_at
                )
            if rebuilt.conflicts:
                raise ValueError("Journal contable incompatible")
        if len(state.conflicts) > 1:
            raise ValueError("Snapshot contiene conflictos incompatibles")
        for conflict in state.conflicts:
            if conflict.detected_at > at:
                raise ValueError("Conflicto futuro en snapshot")
            failed_observation = conflict.observation
            if isinstance(failed_observation, ReplayFact):
                rebuilt = self.receive(rebuilt, failed_observation, at=conflict.detected_at)
            elif isinstance(failed_observation, LedgerClosure):
                rebuilt = self.reconcile_closed(
                    rebuilt, (failed_observation.round_id,), at=conflict.detected_at
                )
            else:
                rebuilt = self.block(rebuilt, conflict.detected_at, conflict.reason)
        if rebuilt != state:
            raise ValueError("Snapshot financiero adulterado o incompleto")

    def report(self, state: LedgerState) -> LedgerReport:
        with localcontext(ledger_context()):
            totals = tuple(
                sum((getattr(r, name) for r in state.round_ledger), Decimal("0"))
                for name in (
                    "gross_realized_pnl_usdc",
                    "open_quantity",
                    "open_cost_basis_usdc",
                    "gross_buy_notional_usdc",
                    "gross_sell_notional_usdc",
                )
            )
        origins = tuple(sorted({e.provenance for e in state.entries}))
        return LedgerReport(
            identity=self.identity,
            gross_realized_pnl_usdc=totals[0],
            open_quantity=totals[1],
            open_cost_basis_usdc=totals[2],
            gross_buy_notional_usdc=totals[3],
            gross_sell_notional_usdc=totals[4],
            round_ledger=state.round_ledger,
            entries=state.entries,
            processed_fill_ids=state.processed_fill_ids,
            reconciliation_status=state.reconciliation_status,
            conflicts=state.conflicts,
            provenances=origins,
            modeled_results=ExecutionOrigin.MODELED in origins,
        )
