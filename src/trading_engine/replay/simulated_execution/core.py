"""Simulador causal puro: ninguna fórmula DCA/TP ni efecto externo."""

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from hashlib import sha256
from uuid import UUID, uuid5

from trading_engine.binance.klines import DEFAULT_SYMBOL, minute_ms
from trading_engine.models._validation import require_type
from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.ids import FillId, IntentId, OrderId
from trading_engine.models.market import Candle
from trading_engine.models.orders import Fill, Order, OrderCancelRequest, OrderIntent
from trading_engine.models.values import Price, Quantity
from trading_engine.replay.encoding import encoded
from trading_engine.replay.execution_model.contracts import assess_ambiguity, price_context
from trading_engine.replay.execution_model.policy import LimitEligibility, PartialFillPolicy
from trading_engine.replay.execution_model.quotes import ModeledQuoteEvidence
from trading_engine.replay.ports import FillEvidence, QuoteEvidence, QuoteSource, ReplayFact
from trading_engine.replay.simulated_execution.policy import (
    AcceptancePolicy,
    QueuePolicy,
    SimulationPolicy,
)
from trading_engine.replay.simulated_execution.state import (
    PROVENANCE,
    TERMINAL,
    ExecutionAudit,
    ExecutionMetrics,
    ExecutionState,
    MinuteLiquidity,
    SimulatedOrder,
    Transition,
    VolumeAllocation,
)
from trading_engine.utils.time import require_utc

# Dominio exclusivamente simulado, distinto de identidad estratégica/operativa.
SIMULATION_NAMESPACE = UUID("4d2fb073-3734-51d0-8a5a-a2c14bbb2f53")
MINUTE = timedelta(minutes=1)


def after(at: datetime, milliseconds: int) -> datetime:
    try:
        return at + timedelta(milliseconds=milliseconds)
    except OverflowError as exc:
        raise ValueError("Timestamp simulado fuera de rango") from exc


@dataclass(frozen=True)
class SimulatedExecution:
    policy: SimulationPolicy
    quotes: QuoteSource

    def __post_init__(self) -> None:
        require_type(self.policy, SimulationPolicy, "policy")

    @property
    def identity(self) -> str:
        return (
            "modeled-execution-v1:"
            + sha256(encoded((self.policy, self.quotes.identity, DEFAULT_SYMBOL))).hexdigest()
        )

    def initial(self) -> ExecutionState:
        return ExecutionState(self.policy, self.identity)

    def _time(self, state: ExecutionState, at: datetime) -> ExecutionState:
        require_utc(at)
        if state.policy != self.policy or state.identity != self.identity:
            raise ValueError("Estado de otro modelo/política/dataset")
        if state.last_at is not None and at < state.last_at:
            raise ValueError("Simulación no permite eventos retrospectivos")
        return replace(state, last_at=at)

    @staticmethod
    def _find(state: ExecutionState, intent_id: IntentId) -> SimulatedOrder:
        return next(o for o in state.orders if o.order.intent_id == intent_id)

    @staticmethod
    def _put(state: ExecutionState, order: SimulatedOrder) -> ExecutionState:
        return replace(
            state,
            orders=tuple(
                order if o.order.intent_id == order.order.intent_id else o for o in state.orders
            ),
        )

    def _fact(
        self, state: ExecutionState, payload: Order | FillEvidence, at: datetime
    ) -> ExecutionState:
        fact = ReplayFact(
            after(at, self.policy.delivery_latency_ms),
            payload,
            state.sequence,
            PROVENANCE,
        )
        return replace(
            state,
            deliveries=state.deliveries + (fact,),
            facts=state.facts + (fact,),
            sequence=state.sequence + 1,
        )

    @staticmethod
    def _audit(
        state: ExecutionState,
        at: datetime,
        kind: str,
        reason: str,
        intent_id: IntentId | None = None,
        candle: Candle | None = None,
        quote: QuoteEvidence | None = None,
        hypothesis: str = "NO_SEQUENCE_ASSUMED",
    ) -> ExecutionState:
        return replace(
            state,
            audit=state.audit
            + (ExecutionAudit(at, kind, intent_id, reason, candle, quote, hypothesis),),
        )

    def receive_intent(
        self, state: ExecutionState, intent: OrderIntent, at: datetime
    ) -> ExecutionState:
        require_type(intent, OrderIntent, "intent")
        state = self._time(state, at)
        if intent.created_at > at:
            raise ValueError("Intención futura")
        previous = next((o for o in state.orders if o.order.intent_id == intent.intent_id), None)
        if previous is not None:
            if previous.order.intent != intent:
                raise ValueError("IntentId conflictivo; no sobrescribir")
            return state
        order_id = OrderId(
            uuid5(SIMULATION_NAMESPACE, self.identity + ":order:" + str(intent.intent_id))
        )
        order = Order(order_id, intent, OrderStatus.PENDING, Decimal("0"), at, at)
        acceptance = after(at, self.policy.acceptance_latency_ms)
        leg = SimulatedOrder(
            order,
            at,
            acceptance,
            after(acceptance, self.policy.execution_delay_ms),
        )
        transition = Transition(acceptance, intent.intent_id, "accept", state.sequence)
        state = replace(
            state,
            orders=state.orders + (leg,),
            transitions=state.transitions + (transition,),
            sequence=state.sequence + 1,
        )
        state = self._fact(state, order, at)
        return self._audit(state, at, "intent_received", "pending_acceptance", intent.intent_id)

    def cancel(
        self, state: ExecutionState, request: OrderCancelRequest, at: datetime
    ) -> ExecutionState:
        require_type(request, OrderCancelRequest, "request")
        state = self._time(state, at)
        if request.created_at > at:
            raise ValueError("Cancelación futura")
        try:
            leg = self._find(state, request.intent_id)
        except StopIteration as exc:
            raise ValueError("Cancelación de intención desconocida") from exc
        if leg.order.intent != request.intent:
            raise ValueError("Contenido de cancelación conflictivo")
        if leg.cancellation is not None:
            if leg.cancellation != request:
                raise ValueError("Retry de cancelación conflictivo")
            return state
        confirmation = after(at, self.policy.cancellation_latency_ms)
        leg = replace(
            leg, cancellation=request, cancel_received_at=at, cancel_confirm_at=confirmation
        )
        state = self._put(state, leg)
        state = replace(
            state,
            transitions=state.transitions
            + (Transition(confirmation, request.intent_id, "cancel", state.sequence),),
            sequence=state.sequence + 1,
        )
        return self._audit(state, at, "cancel_requested", "pending_confirmation", request.intent_id)

    @staticmethod
    def next_at(state: ExecutionState) -> datetime | None:
        times = [t.at for t in state.transitions] + [f.received_at for f in state.deliveries]
        return min(times) if times else None

    def advance(
        self, state: ExecutionState, at: datetime
    ) -> tuple[ExecutionState, tuple[ReplayFact, ...]]:
        next_at = self.next_at(state)
        if next_at is not None and next_at < at:
            raise ValueError("Se omitió un evento de simulación")
        state = self._time(state, at)
        transitions = sorted(
            (t for t in state.transitions if t.at == at),
            key=lambda t: (t.kind != "cancel", t.sequence),
        )
        state = replace(state, transitions=tuple(t for t in state.transitions if t.at != at))
        for transition in transitions:
            leg = self._find(state, transition.intent_id)
            if transition.kind == "cancel":
                if leg.order.status not in TERMINAL:
                    order = replace(leg.order, status=OrderStatus.CANCELED, updated_at=at)
                    leg = replace(leg, order=order)
                    state = self._fact(state, order, at)
                leg = replace(leg, cancel_confirmed_at=at)
                state = self._put(state, leg)
                state = self._audit(
                    state, at, "cancel_confirmed", "final_quantity_known", transition.intent_id
                )
            elif transition.kind == "market_expire":
                if leg.order.status not in TERMINAL:
                    order = replace(leg.order, status=OrderStatus.CANCELED, updated_at=at)
                    leg = replace(leg, order=order)
                    state = self._fact(state, order, at)
                leg = replace(leg, market_resolved_at=at)
                state = self._put(state, leg)
                state = self._audit(
                    state,
                    at,
                    "market_expired",
                    "single_opportunity_remainder_resolved",
                    transition.intent_id,
                )
            elif leg.order.status not in TERMINAL:
                intent = leg.order.intent
                supported = intent.symbol == DEFAULT_SYMBOL and (
                    (intent.side is Side.BUY and not intent.reduce_only)
                    or (
                        intent.side is Side.SELL
                        and intent.reduce_only
                        and intent.order_type is OrderType.LIMIT
                    )
                )
                rejected = not supported or self.policy.acceptance is AcceptancePolicy.REJECT_ALL
                status = OrderStatus.REJECTED if rejected else OrderStatus.OPEN
                order = replace(leg.order, status=status, updated_at=at)
                leg = replace(leg, order=order, accepted_at=None if rejected else at)
                state = self._put(state, leg)
                state = self._fact(state, order, at)
                state = self._audit(
                    state,
                    at,
                    "rejected" if rejected else "accepted",
                    "unsupported_intent" if not supported else self.policy.acceptance.value,
                    intent.intent_id,
                )
        facts = tuple(
            sorted((f for f in state.deliveries if f.received_at == at), key=lambda f: f.key)
        )
        state = replace(state, deliveries=tuple(f for f in state.deliveries if f.received_at != at))
        return state, facts

    def _quote(self, at: datetime) -> QuoteEvidence | None:
        evidence = self.quotes.at(DEFAULT_SYMBOL, at)
        if evidence is None:
            return None
        require_type(evidence, QuoteEvidence, "quote evidence")
        if evidence.quote.timestamp != at or evidence.quote.symbol != DEFAULT_SYMBOL:
            return None
        if isinstance(evidence, ModeledQuoteEvidence) and (
            evidence.policy_identity != self.policy.pricing.identity
            or not evidence.available_at <= at <= evidence.expires_at
        ):
            return None
        return evidence

    def _candidate(self, leg: SimulatedOrder, candle: Candle) -> bool:
        intent = leg.order.intent
        if intent.limit_price is None:
            return False
        return (
            candle.low.value <= intent.limit_price.value
            if intent.side is Side.BUY
            else candle.high.value >= intent.limit_price.value
        )

    def _price(
        self, leg: SimulatedOrder, candle: Candle, quote: QuoteEvidence | None
    ) -> tuple[Price | None, str]:
        intent = leg.order.intent
        with localcontext(price_context()):
            slip = self.policy.pricing.slippage_bps / Decimal("10000")
            if intent.order_type is OrderType.MARKET:
                if quote is None:
                    return None, "missing_valid_quote"
                return Price(quote.quote.ask.value * (1 + slip)), "market_ask_plus_slippage"
            assert intent.limit_price is not None
            limit = intent.limit_price.value
            adjusted_extreme = (
                candle.low.value * (1 + slip)
                if intent.side is Side.BUY
                else candle.high.value * (1 - slip)
            )
            rule = self.policy.pricing.limit_eligibility
            eligible = (
                adjusted_extreme <= limit if intent.side is Side.BUY else adjusted_extreme >= limit
            )
            if rule is not LimitEligibility.TOUCH_CANDIDATE:
                eligible = (
                    adjusted_extreme < limit
                    if intent.side is Side.BUY
                    else adjusted_extreme > limit
                )
            if not eligible:
                return None, "limit_price_not_eligible"
            if rule is LimitEligibility.ADDITIONAL_EVIDENCE:
                if quote is None:
                    return None, "missing_additional_quote_evidence"
                executable = (
                    quote.quote.ask.value * (1 + slip)
                    if intent.side is Side.BUY
                    else quote.quote.bid.value * (1 - slip)
                )
                if not (executable <= limit if intent.side is Side.BUY else executable >= limit):
                    return None, "additional_quote_does_not_support_limit"
            # Sin mejora optimista: el límite exacto es el precio modelado.
            # Slippage restringe la elegibilidad; no se vuelve a cargar al límite.
            return intent.limit_price, "limit_with_queue_and_volume_assumptions"

    def _consume_market(
        self, state: ExecutionState, leg: SimulatedOrder, at: datetime
    ) -> ExecutionState:
        leg = replace(leg, market_evaluated_at=at)
        state = self._put(state, leg)
        return self._audit(
            state, at, "market_opportunity", "single_causal_opportunity", leg.order.intent_id
        )

    def _resolve_market_remainder(
        self, state: ExecutionState, intent_id: IntentId, at: datetime
    ) -> ExecutionState:
        leg = self._find(state, intent_id)
        if leg.order.status in TERMINAL:
            return state
        resolution = after(at, self.policy.execution_delay_ms)
        state = self._put(state, replace(leg, market_resolve_at=resolution))
        state = replace(
            state,
            transitions=state.transitions
            + (Transition(resolution, intent_id, "market_expire", state.sequence),),
            sequence=state.sequence + 1,
        )
        return self._audit(
            state, at, "market_remainder_pending", "awaiting_modeled_expiry", intent_id
        )

    def candle(self, state: ExecutionState, candle: Candle | None, at: datetime) -> ExecutionState:
        state = self._time(state, at)
        minute_ms(at)
        if state.last_minute_at == at:
            prior = next((v for v in state.liquidity if v.candle.close_time == at), None)
            if (prior is None and candle is not None) or (
                prior is not None and prior.candle != candle
            ):
                raise ValueError("Minuto repetido con contenido conflictivo")
            return state
        if state.last_minute_at is not None and at != state.last_minute_at + MINUTE:
            raise ValueError("Cursor de minutos omitido")
        if candle is not None and (
            candle.symbol != DEFAULT_SYMBOL
            or candle.close_time != at
            or candle.open_time != at - MINUTE
        ):
            raise ValueError("Vela futura/inválida para ejecución")
        state = replace(state, last_minute_at=at)
        active = [
            o
            for o in state.orders
            if o.order.status not in TERMINAL and o.market_evaluated_at is None
        ]
        if candle is None:
            for leg in active:
                if (
                    leg.order.intent.order_type is OrderType.MARKET
                    and leg.accepted_at is not None
                    and leg.eligible_from <= at - MINUTE
                ):
                    state = self._consume_market(state, leg, at)
                    state = self._resolve_market_remainder(state, leg.order.intent_id, at)
                state = self._audit(state, at, "no_execution", "gap", leg.order.intent_id)
            return state
        with localcontext(price_context()):
            cap = candle.volume * self.policy.pricing.liquidity.max_volume_participation
        budget = MinuteLiquidity(candle, cap)
        state = replace(state, liquidity=state.liquidity + (budget,))
        eligible = []
        for leg in active:
            if leg.accepted_at is None or leg.eligible_from > candle.open_time:
                state = self._audit(
                    state,
                    at,
                    "no_execution",
                    "not_active_for_full_minute",
                    leg.order.intent_id,
                    candle,
                )
            else:
                eligible.append(leg)
        quote = self._quote(at) if eligible else None
        ambiguous_rounds = {}
        for leg in eligible:
            rid = leg.order.round_id
            candidates = [
                o for o in eligible if o.order.round_id == rid and self._candidate(o, candle)
            ]
            assessment = assess_ambiguity(
                self.policy.pricing.intraminute,
                dca_candidate=any(o.order.intent.side is Side.BUY for o in candidates),
                tp_candidate=any(o.order.intent.side is Side.SELL for o in candidates),
            )
            if assessment.ambiguous and rid not in ambiguous_rounds:
                ambiguous_rounds[rid] = assessment.hypothesis.value
                state = self._audit(
                    state,
                    at,
                    "ambiguity",
                    "intraminute_sequence_unknown",
                    candle=candle,
                    hypothesis=assessment.hypothesis.value,
                )

        def priority(leg: SimulatedOrder) -> tuple[int, datetime, int]:
            hypothesis = ambiguous_rounds.get(leg.order.round_id)
            first = Side.SELL if hypothesis == "TP_FIRST_SENSITIVITY_ONLY" else Side.BUY
            return (
                int(hypothesis is not None and leg.order.intent.side is not first),
                leg.emitted_at,
                leg.order.intent_id.int,
            )

        for original in sorted(eligible, key=priority):
            leg = self._find(state, original.order.intent_id)
            market = leg.order.intent.order_type is OrderType.MARKET
            if market:
                state = self._consume_market(state, leg, at)
                leg = self._find(state, leg.order.intent_id)
            hypothesis = ambiguous_rounds.get(leg.order.round_id, "NO_SEQUENCE_ASSUMED")
            price, reason = self._price(leg, candle, quote)
            if leg.order.round_id in ambiguous_rounds and hypothesis == "NO_SEQUENCE_ASSUMED":
                price, reason = None, "unresolved_intraminute_ambiguity"
            if self.policy.queue is QueuePolicy.WITHHOLD:
                price, reason = None, "queue_evidence_insufficient"
            with localcontext(price_context()):
                available = cap - budget.used
                quantity = min(leg.remaining, available)
                if leg.order.intent.reduce_only:
                    # Sumar hechos canónicos por lado, no cantidades agrupadas
                    # por orden: reagrupar Decimal puede crear residuos artificiales.
                    quantities = []
                    for side in (Side.BUY, Side.SELL):
                        fills = sorted(
                            (
                                f
                                for o in state.orders
                                if o.order.round_id == leg.order.round_id
                                and o.order.intent.side is side
                                for f in o.fills
                            ),
                            key=lambda f: f.fill_id.int,
                        )
                        quantities.append(sum((f.quantity.value for f in fills), Decimal("0")))
                    net = quantities[0] - quantities[1]
                    quantity = min(quantity, max(net, Decimal("0")))
                if quantity <= 0:
                    price, reason = None, "volume_or_reduce_only_capacity_unavailable"
                elif (
                    quantity < leg.remaining
                    and self.policy.pricing.liquidity.partial_fills
                    is PartialFillPolicy.WITHHOLD_IF_UNKNOWN
                ):
                    price, reason = None, "partial_execution_withheld"
                if price is not None:
                    fill_id = FillId(
                        uuid5(
                            SIMULATION_NAMESPACE,
                            self.identity
                            + ":fill:"
                            + str(leg.order.order_id)
                            + ":"
                            + at.isoformat(),
                        )
                    )
                    fill = Fill(fill_id, leg.order.order_id, at, price, Quantity(quantity))
                    total = sum(
                        (
                            f.quantity.value
                            for f in sorted(leg.fills + (fill,), key=lambda f: f.fill_id.int)
                        ),
                        Decimal("0"),
                    )
                    status = (
                        OrderStatus.FILLED
                        if total == leg.order.intent.quantity.value
                        else OrderStatus.PARTIALLY_FILLED
                    )
                    order = replace(leg.order, status=status, filled_quantity=total, updated_at=at)
                    leg = replace(leg, order=order, fills=leg.fills + (fill,))
                    state = self._put(state, leg)
                    budget = replace(
                        budget,
                        allocations=budget.allocations
                        + (VolumeAllocation(order.intent_id, quantity),),
                    )
                    state = replace(state, liquidity=state.liquidity[:-1] + (budget,))
                    state = self._fact(state, FillEvidence(fill, order), at)
            if market:
                state = self._resolve_market_remainder(state, leg.order.intent_id, at)
            state = self._audit(
                state,
                at,
                "fill" if price is not None else "no_execution",
                reason,
                leg.order.intent_id,
                candle,
                quote,
                hypothesis,
            )
        return state

    def validate(self, state: ExecutionState, at: datetime) -> None:
        self._time(state, at)
        if len({o.order.intent_id for o in state.orders}) != len(state.orders):
            raise ValueError("IntentId duplicado")
        next_at = self.next_at(state)
        if next_at is not None and next_at <= at:
            raise ValueError("Snapshot contiene eventos vencidos")
        fills = tuple(f for o in state.orders for f in o.fills)
        if len({f.fill_id for f in fills}) != len(fills):
            raise ValueError("FillId duplicado/conflictivo")
        for leg in state.orders:
            if leg.order.intent.order_type is OrderType.MARKET:
                if len(leg.fills) > 1:
                    raise ValueError("MARKET no permite múltiples oportunidades")
                if leg.market_evaluated_at is not None:
                    evaluated = leg.market_evaluated_at
                    minute_ms(evaluated)
                    if (
                        leg.accepted_at is None
                        or not evaluated - 2 * MINUTE < leg.eligible_from <= evaluated - MINUTE
                        or evaluated > at
                        or any(f.timestamp != evaluated for f in leg.fills)
                    ):
                        raise ValueError("Oportunidad MARKET no causal")
                    if leg.market_resolve_at is not None:
                        if leg.market_resolve_at != after(
                            evaluated, self.policy.execution_delay_ms
                        ):
                            raise ValueError("Resolución MARKET fuera de su transición")
                        pending = [
                            t
                            for t in state.transitions
                            if t.intent_id == leg.order.intent_id and t.kind == "market_expire"
                        ]
                        if leg.market_resolved_at is None:
                            if len(pending) != 1 or pending[0].at != leg.market_resolve_at:
                                raise ValueError("Snapshot perdió la transición terminal MARKET")
                        elif (
                            leg.market_resolved_at != leg.market_resolve_at
                            or leg.market_resolved_at > at
                            or pending
                            or leg.order.status not in TERMINAL
                        ):
                            raise ValueError("Resolución terminal MARKET incompatible")
                    elif leg.order.status is not OrderStatus.FILLED or leg.market_resolved_at:
                        raise ValueError("Remanente MARKET sin resolución")
                elif (
                    leg.fills
                    or leg.market_resolve_at is not None
                    or leg.market_resolved_at is not None
                ):
                    raise ValueError("MARKET ejecutada sin oportunidad registrada")
            elif any(
                value is not None
                for value in (
                    leg.market_evaluated_at,
                    leg.market_resolve_at,
                    leg.market_resolved_at,
                )
            ):
                raise ValueError("LIMIT no admite estado de vida MARKET")
            expected_id = uuid5(
                SIMULATION_NAMESPACE, self.identity + ":order:" + str(leg.order.intent_id)
            )
            if (
                leg.order.order_id != expected_id
                or not leg.emitted_at <= leg.order.updated_at <= at
            ):
                raise ValueError("OrderId/tiempo incompatible")
            if leg.acceptance_at != leg.emitted_at + timedelta(
                milliseconds=self.policy.acceptance_latency_ms
            ):
                raise ValueError("Tiempo de aceptación incompatible")
            if leg.eligible_from != leg.acceptance_at + timedelta(
                milliseconds=self.policy.execution_delay_ms
            ):
                raise ValueError("Tiempo de ejecución incompatible")
            for fill in leg.fills:
                expected_fill = uuid5(
                    SIMULATION_NAMESPACE,
                    self.identity
                    + ":fill:"
                    + str(leg.order.order_id)
                    + ":"
                    + fill.timestamp.isoformat(),
                )
                if (
                    fill.fill_id != expected_fill
                    or fill.order_id != leg.order.order_id
                    or not leg.eligible_from <= fill.timestamp <= at
                ):
                    raise ValueError("FillId/causalidad incompatible")
                limit = leg.order.intent.limit_price
                if limit is not None and (
                    fill.price.value > limit.value
                    if leg.order.intent.side is Side.BUY
                    else fill.price.value < limit.value
                ):
                    raise ValueError("Fill viola precio LIMIT")
            with localcontext(price_context()):
                total = sum(
                    (f.quantity.value for f in sorted(leg.fills, key=lambda f: f.fill_id.int)),
                    Decimal("0"),
                )
            if total != leg.order.filled_quantity or leg.order.updated_at > at:
                raise ValueError("Cantidades/timestamps incompatibles en snapshot")
            if leg.cancellation is not None:
                if leg.cancellation.intent != leg.order.intent or leg.cancel_received_at is None:
                    raise ValueError("Cancelación incompatible")
                if leg.cancel_confirm_at != leg.cancel_received_at + timedelta(
                    milliseconds=self.policy.cancellation_latency_ms
                ):
                    raise ValueError("Tiempo de confirmación incompatible")
        if len({f.sequence for f in state.facts}) != len(state.facts) or any(
            f.provenance != PROVENANCE or f.sequence >= state.sequence for f in state.facts
        ):
            raise ValueError("Secuencia/procedencia de hechos incompatible")
        if any(f not in state.facts for f in state.deliveries):
            raise ValueError("Entrega sin evidencia original")
        if any(v.used > v.cap or v.candle.close_time > at for v in state.liquidity):
            raise ValueError("Cota de volumen/tiempo inválida")
        for budget in state.liquidity:
            with localcontext(price_context()):
                expected_cap = (
                    budget.candle.volume * self.policy.pricing.liquidity.max_volume_participation
                )
            if budget.cap != expected_cap or any(a.quantity <= 0 for a in budget.allocations):
                raise ValueError("Asignación de volumen incompatible")

    def conflict(self, state: ExecutionState, at: datetime, reason: str) -> ExecutionState:
        return self._audit(state, at, "conflict", reason)

    def metrics(self, state: ExecutionState) -> ExecutionMetrics:
        reasons = Counter(a.reason for a in state.audit if a.kind == "no_execution")
        with localcontext(price_context()):
            total = sum((o.order.filled_quantity for o in state.orders), Decimal("0"))
        return ExecutionMetrics(
            state.identity,
            self.policy.pricing.scenario.value,
            len(state.orders),
            sum(o.accepted_at is not None for o in state.orders),
            sum(o.order.status is OrderStatus.REJECTED for o in state.orders),
            sum(o.order.status is OrderStatus.PENDING for o in state.orders),
            sum(o.order.status is OrderStatus.PARTIALLY_FILLED for o in state.orders),
            sum(o.order.status is OrderStatus.FILLED for o in state.orders),
            total,
            sum(not o.fills for o in state.orders),
            sum(o.cancellation is not None for o in state.orders),
            sum(o.cancel_confirmed_at is not None for o in state.orders),
            sum(o.cancellation is not None and o.cancel_confirmed_at is None for o in state.orders),
            sum(len(o.fills) for o in state.orders),
            tuple(sorted(reasons.items())),
            sum(a.kind == "ambiguity" for a in state.audit),
            state.liquidity,
            sum(a.kind == "conflict" for a in state.audit),
        )
