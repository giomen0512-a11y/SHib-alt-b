"""MARKET de oportunidad única: remanente terminal, causalidad y shortfill."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_engine.models.enums import OrderStatus, OrderType
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Order
from trading_engine.models.values import Price
from trading_engine.replay.execution_model.policy import PartialFillPolicy
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayRequest
from trading_engine.replay.ports import FillEvidence, FixtureFacts, FixtureQuotes
from trading_engine.replay.simulated_execution.core import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import QueuePolicy
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, START, SYMBOL, write_db
from .test_simulated_execution import bar, drain, intent, opened, simulation_policy
from .test_simulated_replay import round_candles, setup


def quotes(at_minutes: tuple[int, ...] = (2, 3, 4)) -> FixtureQuotes:
    return FixtureQuotes(
        tuple(
            Quote(
                SYMBOL,
                DUE + timedelta(minutes=n),
                Price(Decimal("99.95")),
                Price(Decimal("100.05")),
            )
            for n in at_minutes
        )
    )


def test_complete_market_single_fill_and_no_expiry() -> None:
    sim = SimulatedExecution(simulation_policy(), quotes())
    at = DUE + timedelta(minutes=2)
    state = sim.candle(opened(sim, intent(kind=OrderType.MARKET, qty="4")), bar(at), at)
    leg = state.orders[0]
    assert leg.order.status is OrderStatus.FILLED
    assert leg.order.filled_quantity == Decimal("4")
    assert leg.market_evaluated_at == at and leg.market_resolve_at is None
    assert len(leg.fills) == 1 and leg.fills[0].price.value == Decimal("100.080015")
    state = drain(sim, state, at)
    sim.validate(state, at)
    later = sim.candle(state, bar(at + timedelta(minutes=1)), at + timedelta(minutes=1))
    assert later.orders[0] == leg


def test_partial_market_preserved_then_canceled_at_distinct_timestamp() -> None:
    sim = SimulatedExecution(simulation_policy(), quotes())
    at = DUE + timedelta(minutes=2)
    state = sim.candle(opened(sim, intent(kind=OrderType.MARKET, qty="8")), bar(at), at)
    assert sim.candle(state, bar(at), at) == state
    leg = state.orders[0]
    assert leg.order.status is OrderStatus.PARTIALLY_FILLED and leg.remaining == Decimal("3")
    assert leg.fills[0].quantity.value == Decimal("5")
    state = drain(sim, state, at)
    sim.validate(state, at)
    resolve = at + timedelta(milliseconds=sim.policy.execution_delay_ms)
    state, delivered = sim.advance(state, resolve)
    leg = state.orders[0]
    assert leg.order.status is OrderStatus.CANCELED and leg.order.filled_quantity == Decimal("5")
    assert leg.order.updated_at == leg.market_resolved_at == resolve > leg.fills[0].timestamp
    assert len(delivered) == 1 and delivered[0].payload == leg.order
    sim.validate(state, resolve)
    for minutes in (3, 4):
        current = DUE + timedelta(minutes=minutes)
        state = sim.candle(state, bar(current, volume="1000"), current)
    assert state.orders[0] == leg
    assert (
        sim.metrics(state).cancellations_requested
        == sim.metrics(state).cancellations_confirmed
        == 0
    )
    snapshots = [f.payload.order for f in state.facts if isinstance(f.payload, FillEvidence)]
    snapshots += [f.payload for f in state.facts if isinstance(f.payload, Order)]
    assert len({o.updated_at for o in snapshots}) == len(snapshots)


@pytest.mark.parametrize("cause", ["quote", "volume", "queue", "partial_policy", "gap"])
def test_no_execution_consumes_opportunity_and_resolves_without_retry(cause: str) -> None:
    policy = simulation_policy()
    source = quotes((3, 4)) if cause == "quote" else quotes()
    if cause == "queue":
        policy = replace(policy, queue=QueuePolicy.WITHHOLD)
    if cause == "partial_policy":
        policy = replace(
            policy,
            pricing=replace(
                policy.pricing,
                liquidity=replace(
                    policy.pricing.liquidity, partial_fills=PartialFillPolicy.WITHHOLD_IF_UNKNOWN
                ),
            ),
        )
    sim = SimulatedExecution(policy, source)
    at = DUE + timedelta(minutes=2)
    candle = None if cause == "gap" else bar(at, volume="0" if cause == "volume" else "100")
    state = sim.candle(opened(sim, intent(kind=OrderType.MARKET, qty="8")), candle, at)
    assert state.orders[0].market_evaluated_at == at and not state.orders[0].fills
    assert state.audit[-1].kind == "no_execution"
    state = drain(sim, state, at + timedelta(milliseconds=100))
    assert state.orders[0].order.status is OrderStatus.CANCELED
    for minute in (3, 4):
        current = DUE + timedelta(minutes=minute)
        state = sim.candle(state, bar(current, volume="10000"), current)
    assert not state.orders[0].fills and state.orders[0].order.filled_quantity == 0


def test_consumed_market_ineligible_even_before_delayed_terminal_transition() -> None:
    policy = replace(simulation_policy(), execution_delay_ms=90000)
    sim = SimulatedExecution(policy, quotes((3, 4, 5)))
    at = DUE + timedelta(minutes=3)
    state = sim.candle(opened(sim, intent(kind=OrderType.MARKET, qty="8")), bar(at), at)
    assert state.orders[0].market_resolve_at == at + timedelta(seconds=90)
    state = drain(sim, state, at)
    later = at + timedelta(minutes=1)
    state = sim.candle(state, bar(later, volume="10000"), later)
    assert state.orders[0].order.status is OrderStatus.PARTIALLY_FILLED
    assert len(state.orders[0].fills) == 1
    state = drain(sim, state, at + timedelta(seconds=90))
    assert state.orders[0].order.status is OrderStatus.CANCELED


@pytest.mark.parametrize("cancel_offset", [0, 50, 100])
def test_external_cancel_and_market_expiry_do_not_duplicate_terminal_snapshot(
    cancel_offset: int,
) -> None:
    from trading_engine.models.orders import OrderCancelRequest

    policy = replace(simulation_policy(), cancellation_latency_ms=100)
    sim = SimulatedExecution(policy, quotes())
    at = DUE + timedelta(minutes=2)
    order = intent(kind=OrderType.MARKET, qty="8")
    state = sim.candle(opened(sim, order), bar(at), at)
    state = drain(sim, state, at)
    request_at = at + timedelta(milliseconds=cancel_offset)
    state = sim.cancel(state, OrderCancelRequest(order, request_at), request_at)
    state = drain(sim, state, at + timedelta(seconds=1))
    terminal = [
        f
        for f in state.facts
        if isinstance(f.payload, Order) and f.payload.status is OrderStatus.CANCELED
    ]
    assert len(terminal) == 1
    assert len(state.orders[0].fills) == 1
    assert state.orders[0].cancel_confirmed_at is not None
    sim.validate(state, at + timedelta(seconds=1))


@pytest.mark.parametrize("damage", ["lost_transition", "wrong_resolution", "lost_opportunity"])
def test_snapshot_requires_market_terminal_transition(damage: str) -> None:
    sim = SimulatedExecution(simulation_policy(), quotes())
    at = DUE + timedelta(minutes=2)
    state = sim.candle(opened(sim, intent(kind=OrderType.MARKET, qty="8")), bar(at), at)
    state = drain(sim, state, at)
    leg = state.orders[0]
    if damage == "lost_transition":
        state = replace(state, transitions=())
    elif damage == "wrong_resolution":
        state = replace(state, orders=(replace(leg, market_resolve_at=at),))
    else:
        state = replace(state, orders=(replace(leg, market_evaluated_at=None),))
    with pytest.raises(ValueError):
        sim.validate(state, at)


@pytest.mark.parametrize("delivery_ms", [0, 500])
@pytest.mark.parametrize("capacity", ["partial", "zero", "full"])
def test_base_market_replay_shortfill_and_fragmented(
    tmp_path: Path, candles: tuple[Candle, ...], delivery_ms: int, capacity: str
) -> None:
    rows = round_candles(candles)
    opportunity = DUE + timedelta(minutes=2)
    volume = {"partial": Decimal("1"), "zero": Decimal("0"), "full": Decimal("1000")}[capacity]
    rows = tuple(
        replace(c, volume=volume, taker_buy_base_volume=volume / Decimal("2"))
        if c.close_time == opportunity
        else c
        for c in rows
    )
    database = write_db(tmp_path / "market-once.sqlite3", rows)
    policy = simulation_policy()
    policy = replace(
        policy, pricing=replace(policy.pricing, quote_latency_ms=0), delivery_latency_ms=delivery_ms
    )
    request = ReplayRequest(START, DUE + timedelta(minutes=5), True)
    with SQLiteHistory(database) as history:
        continuous = setup(history, policy, request, FixtureFacts())
        continuous.run(DUE)
        seed = continuous.snapshot
        expected = continuous.run()
        assert expected.interruption is None and expected.simulation is not None
        strategy = expected.final_state.strategy_state
        assert isinstance(strategy, AltBStrategyState) and strategy.base is not None
        assert len(strategy.base.fills) == (0 if capacity == "zero" else 1)
        if capacity == "partial":
            assert (
                strategy.base.order is not None
                and strategy.base.order.status is OrderStatus.CANCELED
            )
            assert strategy.base.executed_quantity == Decimal("0.05")
            assert strategy.base.average_fill_price is not None
            assert (
                strategy.base.p0 is None
                and strategy.base.block_reason == "base_shortfill_reconciliation"
            )
            assert strategy.round_state is None and len(expected.simulation.orders) == 1
        elif capacity == "zero":
            assert strategy.base.p0 is None and strategy.base.block_reason == "base_order_canceled"
            assert strategy.round_state is None and len(expected.simulation.orders) == 1
        else:
            assert strategy.base.p0 == strategy.base.average_fill_price
            assert strategy.round_state is not None and len(strategy.round_state.dcas) == 7
        part = setup(history, policy, request, FixtureFacts(), seed)
        for cut in (
            opportunity,
            opportunity + timedelta(milliseconds=50),
            opportunity + timedelta(milliseconds=100),
            opportunity + timedelta(milliseconds=500),
        ):
            part.run(cut)
            part = setup(history, policy, request, FixtureFacts(), part.snapshot)
        assert part.run() == expected and part.snapshot == continuous.snapshot
        assert expected.financial_results == "FINANCIAL_RESULTS_NOT_AVAILABLE"


def test_old_rules_are_not_restorable_or_silently_selected() -> None:
    with pytest.raises(ValueError, match="Versión"):
        replace(simulation_policy(), rules_version="closed-minute-shared-budget-v1")


def test_complete_market_rejects_fabricated_resolution_timestamp() -> None:
    sim = SimulatedExecution(simulation_policy(), quotes())
    at = DUE + timedelta(minutes=2)
    state = sim.candle(opened(sim, intent(kind=OrderType.MARKET, qty="4")), bar(at), at)
    state = drain(sim, state, at)
    state = replace(state, orders=(replace(state.orders[0], market_resolved_at=at),))
    with pytest.raises(ValueError, match="sin resolución"):
        sim.validate(state, at)
