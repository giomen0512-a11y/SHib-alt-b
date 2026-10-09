"""Supuestos de ejecución sintéticos; sin mercado, red ni contabilidad financiera."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.ids import IntentId, RoundId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import OrderCancelRequest, OrderIntent
from trading_engine.models.values import Price, Quantity
from trading_engine.replay.execution_model.config import load_profiles
from trading_engine.replay.execution_model.policy import (
    LimitEligibility,
    PartialFillPolicy,
    Scenario,
)
from trading_engine.replay.ports import FixtureQuotes, NoQuotes
from trading_engine.replay.simulated_execution.core import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import (
    AcceptancePolicy,
    QueuePolicy,
    SimulationPolicy,
)
from trading_engine.replay.simulated_execution.state import ExecutionState

from .conftest import DUE, SYMBOL
from .test_execution_policy import FIXTURE_PATH


def simulation_policy(scenario: Scenario = Scenario.BASE) -> SimulationPolicy:
    return SimulationPolicy(
        load_profiles(FIXTURE_PATH).select(scenario),
        acceptance_latency_ms=100,
        execution_delay_ms=100,
        cancellation_latency_ms=120000,
        delivery_latency_ms=0,
        queue=QueuePolicy.BUDGET_FIFO,
        acceptance=AcceptancePolicy.ACCEPT_SUPPORTED,
        assumptions="SYNTHETIC_TEST_ONLY: FIFO budget is an assumption, queue/depth unknown",
    )


def intent(
    number: int = 1,
    *,
    side: Side = Side.BUY,
    kind: OrderType = OrderType.LIMIT,
    qty: str = "4",
    limit: str = "100",
    at: datetime = DUE,
    round_number: int = 100,
) -> OrderIntent:
    return OrderIntent(
        IntentId(UUID(int=number)),
        RoundId(UUID(int=round_number)),
        SYMBOL,
        side,
        kind,
        Quantity(Decimal(qty)),
        at,
        None if kind is OrderType.MARKET else Price(Decimal(limit)),
        side is Side.SELL,
    )


def bar(
    at: datetime, *, low: str = "98", high: str = "102", close: str = "100", volume: str = "100"
) -> Candle:
    return Candle(
        SYMBOL,
        at - timedelta(minutes=1),
        at,
        Price(Decimal(close)),
        Price(Decimal(high)),
        Price(Decimal(low)),
        Price(Decimal(close)),
        Decimal(volume),
    )


def drain(sim: SimulatedExecution, state: ExecutionState, until: datetime) -> ExecutionState:
    while (next_at := sim.next_at(state)) is not None and next_at <= until:
        state, _ = sim.advance(state, next_at)
    return state


def opened(sim: SimulatedExecution, *intents: OrderIntent) -> ExecutionState:
    state = sim.initial()
    for order in intents:
        state = sim.receive_intent(state, order, DUE)
    return drain(sim, state, DUE + timedelta(seconds=1))


@pytest.mark.parametrize(
    "field",
    [
        "acceptance_latency_ms",
        "execution_delay_ms",
        "cancellation_latency_ms",
        "delivery_latency_ms",
    ],
)
@pytest.mark.parametrize("value", [-1, True, 0.5, 10**30])
def test_invalid_latency(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(simulation_policy(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field", ["acceptance_latency_ms", "execution_delay_ms", "cancellation_latency_ms"]
)
def test_positive_transition_latency_required(field: str) -> None:
    with pytest.raises(ValueError):
        replace(simulation_policy(), **{field: 0})  # type: ignore[arg-type]


@pytest.mark.parametrize("scenario", list(Scenario))
def test_each_scenario_deterministic_identity(scenario: Scenario) -> None:
    p = simulation_policy(scenario)
    assert p.identity == simulation_policy(scenario).identity
    assert p.identity != replace(p, delivery_latency_ms=1).identity
    assert SimulatedExecution(p, NoQuotes()).identity == SimulatedExecution(p, NoQuotes()).identity


def test_pending_acceptance_and_no_early_market_fill() -> None:
    p = replace(simulation_policy(), acceptance_latency_ms=120000)
    sim = SimulatedExecution(p, NoQuotes())
    state = sim.receive_intent(sim.initial(), intent(kind=OrderType.MARKET), DUE)
    assert state.orders[0].order.status is OrderStatus.PENDING
    state = drain(sim, state, DUE + timedelta(minutes=1))
    state = sim.candle(state, bar(DUE + timedelta(minutes=1)), DUE + timedelta(minutes=1))
    assert not state.orders[0].fills and state.orders[0].accepted_at is None
    with pytest.raises(ValueError, match="futura"):
        sim.receive_intent(sim.initial(), intent(at=DUE + timedelta(seconds=1)), DUE)


@pytest.mark.parametrize("quote_kind", ["none", "past", "future", "valid"])
def test_market_quote_required_and_spread_slippage_once(quote_kind: str) -> None:
    at = DUE + timedelta(minutes=2)
    timestamp = at + timedelta(seconds={"past": -1, "future": 1}.get(quote_kind, 0))
    source = (
        NoQuotes()
        if quote_kind == "none"
        else FixtureQuotes(
            (Quote(SYMBOL, timestamp, Price(Decimal("99.95")), Price(Decimal("100.05"))),)
        )
    )
    sim = SimulatedExecution(simulation_policy(), source)
    state = opened(sim, intent(kind=OrderType.MARKET, qty="1"))
    state = sim.candle(state, bar(at), at)
    fills = state.orders[0].fills
    if quote_kind == "valid":
        assert fills[0].price.value == Decimal("100.080015")
        assert fills[0].timestamp >= state.orders[0].eligible_from > DUE
    else:
        assert not fills
        assert state.audit[-1].reason == "missing_valid_quote"


@pytest.mark.parametrize(
    "side,low,high,executed",
    [
        (Side.BUY, "98", "100", True),
        (Side.BUY, "101", "102", False),
        (Side.SELL, "100", "102", True),
        (Side.SELL, "98", "99", False),
    ],
)
def test_limit_eligibility_and_price_protection(
    side: Side, low: str, high: str, executed: bool
) -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    orders = [intent(side=side)]
    if side is Side.SELL:
        orders.insert(0, intent(2, kind=OrderType.MARKET, qty="4"))
        at = DUE + timedelta(minutes=2)
        sim = SimulatedExecution(
            simulation_policy(),
            FixtureQuotes((Quote(SYMBOL, at, Price(Decimal("100")), Price(Decimal("100"))),)),
        )
        state = opened(sim, orders[0])
        state = sim.candle(state, bar(at), at)
        state = drain(sim, state, at)
        sell = replace(orders[1], created_at=at)
        state = sim.receive_intent(state, sell, at)
        state = drain(sim, state, at + timedelta(seconds=1))
        state = sim.candle(
            state,
            bar(at + timedelta(minutes=1), low=low, high=high, close=low),
            at + timedelta(minutes=1),
        )
        at += timedelta(minutes=2)
    else:
        state = opened(sim, orders[0])
        at = DUE + timedelta(minutes=2)
    state = sim.candle(state, bar(at, low=low, high=high, close=low), at)
    leg = next(o for o in state.orders if o.order.intent.side is side)
    assert bool(leg.fills) is executed
    if executed:
        assert leg.fills[0].price == leg.order.intent.limit_price


def test_order_activated_during_minute_cannot_use_previous_path() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    state = opened(sim, intent())
    at = DUE + timedelta(minutes=1)
    state = sim.candle(state, bar(at), at)
    assert not state.orders[0].fills
    assert state.audit[-1].reason == "not_active_for_full_minute"


@pytest.mark.parametrize(
    "rule,fill",
    [(LimitEligibility.TOUCH_CANDIDATE, True), (LimitEligibility.STRICT_CROSS_CANDIDATE, False)],
)
def test_touch_vs_strict_cross_explicit(rule: LimitEligibility, fill: bool) -> None:
    p = simulation_policy()
    p = replace(p, pricing=replace(p.pricing, slippage_bps=Decimal("0"), limit_eligibility=rule))
    sim = SimulatedExecution(p, NoQuotes())
    state = opened(sim, intent())
    at = DUE + timedelta(minutes=2)
    state = sim.candle(state, bar(at, low="100", high="101"), at)
    assert bool(state.orders[0].fills) is fill


@pytest.mark.parametrize("quote", [False, True])
def test_additional_evidence_policy_requires_supporting_quote(quote: bool) -> None:
    p = simulation_policy()
    p = replace(
        p, pricing=replace(p.pricing, limit_eligibility=LimitEligibility.ADDITIONAL_EVIDENCE)
    )
    at = DUE + timedelta(minutes=2)
    source = (
        FixtureQuotes((Quote(SYMBOL, at, Price(Decimal("98")), Price(Decimal("99"))),))
        if quote
        else NoQuotes()
    )
    sim = SimulatedExecution(p, source)
    state = sim.candle(opened(sim, intent()), bar(at), at)
    assert bool(state.orders[0].fills) is quote


@pytest.mark.parametrize(
    "volume,queue,partial,expected",
    [
        ("0", QueuePolicy.BUDGET_FIFO, PartialFillPolicy.EVIDENCE_REQUIRED, "0"),
        ("100", QueuePolicy.WITHHOLD, PartialFillPolicy.EVIDENCE_REQUIRED, "0"),
        ("100", QueuePolicy.BUDGET_FIFO, PartialFillPolicy.EVIDENCE_REQUIRED, "5"),
        ("100", QueuePolicy.BUDGET_FIFO, PartialFillPolicy.WITHHOLD_IF_UNKNOWN, "4"),
    ],
)
def test_two_dcas_share_budget_and_partial_policy(
    volume: str, queue: QueuePolicy, partial: PartialFillPolicy, expected: str
) -> None:
    p = simulation_policy()
    p = replace(
        p,
        queue=queue,
        pricing=replace(p.pricing, liquidity=replace(p.pricing.liquidity, partial_fills=partial)),
    )
    sim = SimulatedExecution(p, NoQuotes())
    state = opened(sim, intent(1), intent(2))
    at = DUE + timedelta(minutes=2)
    state = sim.candle(state, bar(at, volume=volume), at)
    assert state.liquidity[-1].used == Decimal(expected) <= state.liquidity[-1].cap
    assert all(o.order.filled_quantity <= o.order.intent.quantity.value for o in state.orders)
    assert all(a.queue_unknown and a.depth_unknown for a in state.audit)


def test_partial_then_complete_remaining_never_exceeded() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    state = opened(sim, intent(qty="8"))
    at = DUE + timedelta(minutes=2)
    state = sim.candle(state, bar(at), at)
    assert state.orders[0].order.status is OrderStatus.PARTIALLY_FILLED
    assert state.orders[0].remaining == Decimal("3")
    state = drain(sim, state, at)
    at += timedelta(minutes=1)
    state = sim.candle(state, bar(at), at)
    assert [f.quantity.value for f in state.orders[0].fills] == [Decimal("5"), Decimal("3")]
    assert state.orders[0].order.status is OrderStatus.FILLED and state.orders[0].remaining == 0
    state = drain(sim, state, at)
    sim.validate(state, at)


def test_missing_minute_no_interpolation_or_fill() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    state = opened(sim, intent())
    at = DUE + timedelta(minutes=2)
    state = sim.candle(state, None, at)
    assert not state.orders[0].fills and state.audit[-1].reason == "gap"
    state = sim.candle(state, bar(at + timedelta(minutes=1)), at + timedelta(minutes=1))
    assert len(state.orders[0].fills) == 1


def test_idempotence_and_conflicting_intent_cancel_minute() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    order = intent()
    state = sim.receive_intent(sim.initial(), order, DUE)
    assert sim.receive_intent(state, order, DUE) == state
    with pytest.raises(ValueError, match="conflictivo"):
        sim.receive_intent(state, replace(order, quantity=Quantity(Decimal("9"))), DUE)
    state = drain(sim, state, DUE + timedelta(seconds=1))
    request = OrderCancelRequest(order, DUE + timedelta(seconds=1))
    state = sim.cancel(state, request, request.created_at)
    assert sim.cancel(state, request, request.created_at) == state
    with pytest.raises(ValueError, match="conflictivo"):
        sim.cancel(
            state,
            replace(request, created_at=request.created_at + timedelta(seconds=1)),
            request.created_at + timedelta(seconds=1),
        )
    at = DUE + timedelta(minutes=2)
    state = sim.candle(state, bar(at), at)
    assert sim.candle(state, bar(at), at) == state
    with pytest.raises(ValueError, match="conflictivo"):
        sim.candle(state, bar(at, volume="50"), at)


def test_cancel_pending_can_fill_before_terminal_confirmation() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    order = intent(qty="8")
    state = opened(sim, order)
    request_at = DUE + timedelta(seconds=1)
    request = OrderCancelRequest(order, request_at)
    state = sim.cancel(state, request, request_at)
    at = DUE + timedelta(minutes=2)
    state = sim.candle(state, bar(at), at)
    assert state.orders[0].order.filled_quantity == Decimal("5")
    assert state.orders[0].cancel_confirmed_at is None
    state = drain(sim, state, request_at + timedelta(minutes=2))
    assert state.orders[0].order.status is OrderStatus.CANCELED
    assert state.orders[0].order.filled_quantity == Decimal("5")
    state = sim.candle(state, bar(at + timedelta(minutes=1)), at + timedelta(minutes=1))
    assert len(state.orders[0].fills) == 1


def test_cancel_before_acceptance_has_no_reopening() -> None:
    p = replace(simulation_policy(), acceptance_latency_ms=1000, cancellation_latency_ms=500)
    sim = SimulatedExecution(p, NoQuotes())
    order = intent()
    state = sim.receive_intent(sim.initial(), order, DUE)
    state = sim.cancel(state, OrderCancelRequest(order, DUE), DUE)
    state = drain(sim, state, DUE + timedelta(seconds=2))
    assert state.orders[0].order.status is OrderStatus.CANCELED
    assert state.orders[0].accepted_at is None


def test_modeled_rejection_and_no_execution() -> None:
    sim = SimulatedExecution(
        replace(simulation_policy(), acceptance=AcceptancePolicy.REJECT_ALL), NoQuotes()
    )
    state = opened(sim, intent())
    assert state.orders[0].order.status is OrderStatus.REJECTED
    at = DUE + timedelta(minutes=2)
    state = sim.candle(state, bar(at), at)
    assert sim.metrics(state).orders_rejected == 1 and sim.metrics(state).fills_generated == 0


def test_decimal_context_independence_and_future_candle_rejected() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    state = opened(sim, intent(qty="8.123456789"))
    at = DUE + timedelta(minutes=2)
    candle = bar(at, volume="12.123456789")
    expected = sim.candle(state, candle, at)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert sim.candle(state, candle, at) == expected
        assert context.prec == 2
    with pytest.raises(ValueError, match="futura"):
        sim.candle(state, bar(at + timedelta(minutes=1)), at)


def test_delivery_separates_occurrence_and_availability() -> None:
    sim = SimulatedExecution(replace(simulation_policy(), delivery_latency_ms=500), NoQuotes())
    state = sim.receive_intent(sim.initial(), intent(), DUE)
    state, facts = sim.advance(state, DUE + timedelta(milliseconds=100))
    assert not facts
    assert state.orders[0].accepted_at == DUE + timedelta(milliseconds=100)
    state, facts = sim.advance(state, DUE + timedelta(milliseconds=500))
    assert len(facts) == 1 and facts[0].provenance == "MODELED_EXECUTION"
    state, facts = sim.advance(state, DUE + timedelta(milliseconds=600))
    assert facts[0].payload == state.orders[0].order


def test_invalid_restoration_and_skipped_event_rejected() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    state = sim.receive_intent(sim.initial(), intent(), DUE)
    with pytest.raises(ValueError, match="omitió"):
        sim.advance(state, DUE + timedelta(seconds=1))
    with pytest.raises(ValueError, match="vencidos"):
        sim.validate(state, DUE)
    with pytest.raises(ValueError, match="otro"):
        SimulatedExecution(replace(sim.policy, delivery_latency_ms=1), NoQuotes()).validate(
            state, DUE
        )


@pytest.mark.parametrize("scenario", list(Scenario))
def test_dca_tp_ambiguity_and_shared_cap(scenario: Scenario) -> None:
    p = simulation_policy(scenario)
    p = replace(
        p,
        pricing=replace(
            p.pricing,
            liquidity=replace(
                p.pricing.liquidity,
                max_volume_participation=Decimal("0.05"),
                partial_fills=PartialFillPolicy.EVIDENCE_REQUIRED,
            ),
            limit_eligibility=LimitEligibility.STRICT_CROSS_CANDIDATE,
        ),
    )
    first = DUE + timedelta(minutes=2)
    source = FixtureQuotes((Quote(SYMBOL, first, Price(Decimal("100")), Price(Decimal("100"))),))
    sim = SimulatedExecution(p, source)
    state = opened(sim, intent(1, kind=OrderType.MARKET, qty="4"))
    state = sim.candle(state, bar(first), first)
    state = drain(sim, state, first)
    for order in (
        intent(2, qty="4", limit="99", at=first),
        intent(3, side=Side.SELL, qty="4", limit="101", at=first),
    ):
        state = sim.receive_intent(state, order, first)
    state = drain(sim, state, first + timedelta(seconds=1))
    state = sim.candle(state, bar(first + timedelta(minutes=1)), first + timedelta(minutes=1))
    at = first + timedelta(minutes=2)
    state = sim.candle(state, bar(at, low="97", high="103"), at)
    assert state.liquidity[-1].used <= Decimal("5")
    assert sim.metrics(state).ambiguities == 1
    if scenario is Scenario.BASE:
        assert state.liquidity[-1].used == 0
        assert not state.orders[1].fills and not state.orders[2].fills
    else:
        assert state.liquidity[-1].used == Decimal("5")
        expected = (
            "TP_FIRST_SENSITIVITY_ONLY"
            if scenario is Scenario.FAVORABLE
            else "DCA_FIRST_SENSITIVITY_ONLY"
        )
        assert state.audit[-1].hypothesis == expected
        expected_first = (
            state.orders[2].order.intent_id
            if scenario is Scenario.FAVORABLE
            else state.orders[1].order.intent_id
        )
        assert state.liquidity[-1].allocations[0].intent_id == expected_first


@pytest.mark.parametrize(
    "change", ["fill_id", "fill_order", "filled", "cap", "duplicate_fill", "provenance"]
)
def test_snapshot_execution_invariants_reject_tampering(change: str) -> None:
    from trading_engine.models.ids import FillId, OrderId

    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    at = DUE + timedelta(minutes=2)
    state = sim.candle(opened(sim, intent(qty="8")), bar(at), at)
    state = drain(sim, state, at)
    sim.validate(state, at)
    leg = state.orders[0]
    if change == "cap":
        state = replace(state, liquidity=(replace(state.liquidity[0], cap=Decimal("100")),))
    elif change == "provenance":
        state = replace(
            state, facts=(replace(state.facts[0], provenance="REAL_OBSERVED"),) + state.facts[1:]
        )
    else:
        if change == "fill_id":
            leg = replace(leg, fills=(replace(leg.fills[0], fill_id=FillId(UUID(int=999))),))
        elif change == "fill_order":
            leg = replace(leg, fills=(replace(leg.fills[0], order_id=OrderId(UUID(int=999))),))
        elif change == "filled":
            leg = replace(leg, order=replace(leg.order, filled_quantity=Decimal("6")))
        else:
            leg = replace(leg, fills=leg.fills + leg.fills)
        state = replace(state, orders=(leg,))
    with pytest.raises(ValueError):
        sim.validate(state, at)


def test_unknown_cancel_and_unsupported_intent_explicitly_rejected() -> None:
    sim = SimulatedExecution(simulation_policy(), NoQuotes())
    with pytest.raises(ValueError, match="desconocida"):
        sim.cancel(sim.initial(), OrderCancelRequest(intent(), DUE), DUE)
    unsupported = replace(intent(), side=Side.SELL, reduce_only=False)
    state = opened(sim, unsupported)
    assert state.orders[0].order.status is OrderStatus.REJECTED
    assert state.audit[-1].reason == "unsupported_intent"


def test_fill_ids_do_not_change_on_duplicate_candle_or_reconstruction() -> None:
    p = simulation_policy()
    first = SimulatedExecution(p, NoQuotes())
    other = SimulatedExecution(p, NoQuotes())
    at = DUE + timedelta(minutes=2)
    state = first.candle(opened(first, intent()), bar(at), at)
    assert state == other.candle(opened(other, intent()), bar(at), at)
    assert first.candle(state, bar(at), at).orders[0].fills == state.orders[0].fills
