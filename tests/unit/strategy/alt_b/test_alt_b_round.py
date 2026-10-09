"""Rondas offline: siete DCA, TP sin solapamiento y cierre con evidencia."""

import ast
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.ids import FillId, OrderId, RoundId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order, OrderCancelRequest, OrderIntent
from trading_engine.models.values import Price, Quantity
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinationResult, CoordinatorState
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.engine import AltBEngine
from trading_engine.strategy.alt_b.events.execution_state import OrderLeg
from trading_engine.strategy.alt_b.events.round_state import AltBRoundState
from trading_engine.strategy.alt_b.events.state import (
    AltBStrategyState,
    PendingEntry,
    entry_identity,
)
from trading_engine.strategy.alt_b.math import order_margins
from trading_engine.strategy.alt_b.state_market import AltBMarketState


def state_of(state: CoordinatorState) -> AltBStrategyState:
    assert isinstance(state.strategy_state, AltBStrategyState)
    return state.strategy_state


def round_of(state: CoordinatorState) -> AltBRoundState:
    management = state_of(state).round_state
    assert management is not None
    return management


def opened_at(state: CoordinatorState) -> datetime:
    at = state_of(state).last_event_at
    assert at is not None
    return at


def order_for(
    intent: OrderIntent,
    number: int,
    at: datetime,
    status: OrderStatus = OrderStatus.OPEN,
    qty: Decimal = Decimal("0"),
) -> Order:
    return Order(OrderId(UUID(int=number)), intent, status, qty, intent.created_at, at)


def fill_for(
    order: Order, number: int, qty: Decimal, price: str, at: datetime | None = None
) -> Fill:
    return Fill(
        FillId(UUID(int=number)),
        order.order_id,
        at or order.updated_at,
        Price(Decimal(price)),
        Quantity(qty),
    )


@pytest.fixture
def base_pending(history: tuple[Candle, ...], checkpoint: datetime) -> CoordinatorState:
    signal, action = entry_identity(AltBConfig(), checkpoint)
    logical = AltBStrategyState(
        AltBMarketState(history), True, PendingEntry(signal, action), checkpoint
    )
    coordinator = StrategyCoordinator(AltBEngine())
    q = Quote(action.symbol, action.not_before, Price(Decimal("99")), Price(Decimal("100")))
    return coordinator.on_scheduled_action(CoordinatorState((action,), logical), action, q).state


@pytest.fixture
def initial(base_pending: CoordinatorState) -> CoordinationResult:
    base = state_of(base_pending).base
    assert base is not None
    at = base.intent.created_at + timedelta(seconds=1)
    order = order_for(base.intent, 100, at, OrderStatus.FILLED, base.intent.quantity.value)
    fill = fill_for(order, 1000, order.filled_quantity, "100")
    return StrategyCoordinator(AltBEngine()).on_fill(base_pending, fill, order)


@pytest.fixture
def tp_accepted(initial: CoordinationResult) -> CoordinatorState:
    intent = round_of(initial.state).take_profits[0].intent
    order = order_for(intent, 200, opened_at(initial.state) + timedelta(seconds=1))
    return StrategyCoordinator(AltBEngine()).on_order(initial.state, order).state


def dca_fill(
    state: CoordinatorState, level: int = 1, qty: str = "0.25", price: str = "98"
) -> CoordinationResult:
    intent = round_of(state).dcas[level - 1].intent
    at = opened_at(state) + timedelta(seconds=1)
    order = order_for(intent, 300 + level, at, OrderStatus.PARTIALLY_FILLED, Decimal(qty))
    fill = fill_for(order, 2000 + level, order.filled_quantity, price)
    return StrategyCoordinator(AltBEngine()).on_fill(state, fill, order)


def cancel_tp(state: CoordinatorState, filled: Decimal = Decimal("0")) -> CoordinationResult:
    intent = round_of(state).take_profits[-1].intent
    order = order_for(
        intent, 200, opened_at(state) + timedelta(seconds=1), OrderStatus.CANCELED, filled
    )
    return StrategyCoordinator(AltBEngine()).on_order(state, order)


def close_exposure(state: CoordinatorState) -> CoordinationResult:
    intent = round_of(state).take_profits[-1].intent
    order = order_for(
        intent,
        200,
        opened_at(state) + timedelta(seconds=1),
        OrderStatus.FILLED,
        intent.quantity.value,
    )
    fill = fill_for(order, 5000, order.filled_quantity, "101")
    return StrategyCoordinator(AltBEngine()).on_fill(state, fill, order)


def settle_dcas(state: CoordinatorState) -> CoordinatorState:
    coordinator = StrategyCoordinator(AltBEngine())
    for index in range(7):
        leg = round_of(state).dcas[index]
        at = opened_at(state) + timedelta(seconds=1)
        order = order_for(leg.intent, 301 + index, at, OrderStatus.CANCELED, leg.executed_quantity)
        state = coordinator.on_order(state, order).state
    return state


def test_confirmed_base_emits_exactly_seven_dcas_and_initial_tp(
    initial: CoordinationResult,
) -> None:
    management = round_of(initial.state)
    assert len(initial.order_intents) == 8
    assert initial.order_intents[:7] == tuple(leg.intent for leg in management.dcas)
    assert initial.order_intents[7] == management.take_profits[0].intent
    assert management.p0 == Price(Decimal("100"))
    assert all(not leg.fills and leg.order is None for leg in management.legs)
    assert not initial.cancel_requests


@pytest.mark.parametrize(
    "level,drop",
    [
        (1, "0.015"),
        (2, "0.03225"),
        (3, "0.0520875"),
        (4, "0.074900625"),
        (5, "0.10113571875"),
        (6, "0.1313060765625"),
        (7, "0.166001988046875"),
    ],
)
def test_seven_exact_prices_anchored_to_same_p0(
    initial: CoordinationResult, level: int, drop: str
) -> None:
    leg = round_of(initial.state).dcas[level - 1]
    with localcontext() as ctx:
        ctx.prec = 50
        expected_price = Decimal("100") * (1 - Decimal(drop))
        margin = Decimal("1200") * 69 * Decimal("1.40") ** (level - 1) / Decimal("1704.882944")
        expected_qty = margin * 4 / expected_price
    assert leg.intent.limit_price == Price(expected_price)
    assert leg.intent.quantity.value == expected_qty
    assert leg.intent.side is Side.BUY and leg.intent.order_type is OrderType.LIMIT
    assert leg.intent.reduce_only is False and leg.intent.intent_id.version == 5


def test_margins_preserve_budget_and_hostile_context_does_not_change_sizing(
    base_pending: CoordinatorState, initial: CoordinationResult
) -> None:
    base = state_of(base_pending).base
    assert base is not None
    at = base.intent.created_at + timedelta(seconds=1)
    order = order_for(base.intent, 100, at, OrderStatus.FILLED, base.intent.quantity.value)
    fill = fill_for(order, 1000, order.filled_quantity, "100")
    with localcontext() as ctx:
        ctx.prec = 50
        assert sum(order_margins(AltBConfig()), Decimal("0")) == Decimal("1200")
    with localcontext() as ctx:
        ctx.prec = 4
        ctx.rounding = ROUND_UP
        replay = StrategyCoordinator(AltBEngine()).on_fill(base_pending, fill, order)
        assert ctx.prec == 4 and ctx.rounding == ROUND_UP
    assert replay == initial


@pytest.mark.parametrize("condition", ["pending", "partial", "canceled_partial", "unreconciled"])
def test_unresolved_base_cannot_create_dca_or_tp(
    base_pending: CoordinatorState, condition: str
) -> None:
    base = state_of(base_pending).base
    assert base is not None
    at = base.intent.created_at + timedelta(seconds=1)
    status = {
        "pending": OrderStatus.OPEN,
        "partial": OrderStatus.PARTIALLY_FILLED,
        "canceled_partial": OrderStatus.CANCELED,
        "unreconciled": OrderStatus.FILLED,
    }[condition]
    qty = (
        Decimal("0")
        if condition == "pending"
        else base.intent.quantity.value
        if condition == "unreconciled"
        else Decimal("0.5")
    )
    order = order_for(base.intent, 100, at, status, qty)
    coordinator = StrategyCoordinator(AltBEngine())
    result = coordinator.on_order(base_pending, order)
    if condition in ("partial", "canceled_partial"):
        result = coordinator.on_fill(result.state, fill_for(order, 1000, qty, "100"), order)
    assert not result.order_intents and state_of(result.state).round_state is None
    assert state_of(result.state).base is not None


def test_quote_never_reanchors_p0_or_manufactures_fills(initial: CoordinationResult) -> None:
    logical = state_of(initial.state)
    assert logical.base is not None
    q = Quote(
        logical.base.intent.symbol,
        opened_at(initial.state) + timedelta(seconds=1),
        Price(Decimal("1")),
        Price(Decimal("2")),
    )
    result = StrategyCoordinator(AltBEngine()).on_quote(initial.state, q)
    assert round_of(result.state) == round_of(initial.state)
    assert not result.order_intents


def test_fill_retry_and_reconstructed_snapshot_never_duplicate_levels(
    initial: CoordinationResult,
) -> None:
    base = state_of(initial.state).base
    assert base is not None and base.order is not None
    coordinator = StrategyCoordinator(AltBEngine())
    result = coordinator.on_fill(initial.state, base.fills[0], base.order)
    assert result.state == initial.state and not result.order_intents
    restored = replace(
        initial.state,
        strategy_state=replace(
            state_of(initial.state), round_state=replace(round_of(initial.state))
        ),
    )
    assert coordinator.on_order(restored, base.order).state == initial.state
    assert tuple(f.name for f in fields(AltBEngine())) == ("config",)


def test_initial_tp_is_sell_reduce_only_for_real_quantity(initial: CoordinationResult) -> None:
    management = round_of(initial.state)
    base = state_of(initial.state).base
    assert base is not None
    tp = management.take_profits[0].intent
    assert tp.side is Side.SELL and tp.order_type is OrderType.LIMIT and tp.reduce_only
    assert tp.quantity.value == base.executed_quantity
    assert tp.limit_price == Price(Decimal("100.800"))


def test_dca_fill_changes_average_target_but_not_p0_or_remaining_levels(
    tp_accepted: CoordinatorState,
) -> None:
    original = round_of(tp_accepted)
    result = dca_fill(tp_accepted)
    management = round_of(result.state)
    base = state_of(result.state).base
    assert base is not None
    with localcontext() as ctx:
        ctx.prec = 50
        expected = (base.executed_quantity * 100 + Decimal("0.25") * 98) / (
            base.executed_quantity + Decimal("0.25")
        )
        expected_tp = expected * Decimal("1.008")
    assert management.average_entry_price(base) == Price(expected)
    assert management.tp_target(base) == Price(expected_tp)
    assert management.p0 == original.p0
    assert tuple(leg.intent for leg in management.dcas) == tuple(
        leg.intent for leg in original.dcas
    )
    assert len(result.cancel_requests) == 1 and not result.order_intents
    assert result.cancel_requests[0].intent == original.take_profits[0].intent
    assert management.take_profits[0].order == original.take_profits[0].order


def test_no_new_tp_until_cancel_is_terminal_and_fills_reconciled(
    tp_accepted: CoordinatorState,
) -> None:
    changed = dca_fill(tp_accepted)
    waiting = cancel_tp(changed.state, Decimal("0.1"))
    assert not waiting.order_intents and len(round_of(waiting.state).take_profits) == 1
    leg = round_of(waiting.state).take_profits[0]
    assert leg.order is not None
    late = fill_for(leg.order, 4000, Decimal("0.1"), "101", at=opened_at(changed.state))
    result = StrategyCoordinator(AltBEngine()).on_fill(waiting.state, late, leg.order)
    management = round_of(result.state)
    base = state_of(result.state).base
    assert base is not None
    assert len(result.order_intents) == 1 and len(management.take_profits) == 2
    replacement = result.order_intents[0]
    assert replacement.quantity.value == management.quantity_open(base)
    assert replacement.limit_price == management.tp_target(base)
    assert replacement.intent_id != leg.intent.intent_id and replacement.reduce_only
    assert late in management.exit_fills
    retry = StrategyCoordinator(AltBEngine()).on_fill(result.state, late, leg.order)
    assert not retry.order_intents and retry.state == result.state


def test_cancel_of_unacknowledged_tp_is_by_intent_not_invented_order_id(
    initial: CoordinationResult,
) -> None:
    changed = dca_fill(initial.state)
    assert len(changed.cancel_requests) == 1
    tp = round_of(changed.state).take_profits[0]
    assert tp.order is None and not changed.order_intents
    confirmed = cancel_tp(changed.state)
    assert len(confirmed.order_intents) == 1


def test_partial_exit_reduces_net_quantity_without_closing(tp_accepted: CoordinatorState) -> None:
    management = round_of(tp_accepted)
    tp = management.take_profits[0]
    assert tp.order is not None
    at = opened_at(tp_accepted) + timedelta(seconds=1)
    order = replace(
        tp.order,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=Decimal("0.25"),
        updated_at=at,
    )
    fill = fill_for(order, 4000, Decimal("0.25"), "101")
    result = StrategyCoordinator(AltBEngine()).on_fill(tp_accepted, fill, order)
    base = state_of(result.state).base
    assert base is not None
    with localcontext() as ctx:
        ctx.prec = 50
        expected = base.executed_quantity - Decimal("0.25")
    assert round_of(result.state).quantity_open(base) == expected
    assert round_of(result.state).closed_at is None and not result.order_intents
    assert not result.cancel_requests


def test_sold_quantity_zero_keeps_reservation_until_entry_cancellations(
    tp_accepted: CoordinatorState,
) -> None:
    result = close_exposure(tp_accepted)
    logical = state_of(result.state)
    assert logical.base is not None
    management = round_of(result.state)
    assert management.quantity_open(logical.base) == 0 and management.closing
    assert management.closed_at is None and len(result.cancel_requests) == 7
    assert not result.order_intents and not logical.round_history


def test_round_closes_only_after_all_orders_are_terminal_with_exact_fills(
    tp_accepted: CoordinatorState,
) -> None:
    closing = close_exposure(tp_accepted)
    closed = settle_dcas(closing.state)
    logical = state_of(closed)
    assert logical.base is None and logical.round_state is None and len(logical.round_history) == 1
    record = logical.round_history[0]
    assert record.round_state.closed_at == opened_at(closed)
    assert record.round_state.quantity_open(record.base) == 0
    assert record.round_state.can_close(record.base)
    assert (
        len(record.round_state.entry_fills(record.base)) == len(record.round_state.exit_fills) == 1
    )


def test_pause_and_new_veto_do_not_delete_or_cancel_open_round(
    tp_accepted: CoordinatorState, history: tuple[Candle, ...]
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    pause = coordinator.on_entry_control(tp_accepted, False, opened_at(tp_accepted))
    assert round_of(pause.state) == round_of(tp_accepted)
    assert not pause.cancel_requests
    # Futuro checkpoint sintético falla filtros: no afecta las órdenes de la ronda.
    at = opened_at(pause.state).replace(second=0, microsecond=0) + timedelta(minutes=15)
    c = replace(
        history[-1],
        open_time=at,
        close_time=at + timedelta(minutes=1),
        low=Price(Decimal("1")),
        close=Price(Decimal("1")),
    )
    observed = coordinator.on_candle(pause.state, c)
    assert round_of(observed.state) == round_of(tp_accepted)
    assert not observed.signals and not observed.cancel_requests
    filled = dca_fill(observed.state)
    assert not state_of(filled.state).allow_entries
    assert len(filled.cancel_requests) == 1


def test_same_fill_id_is_idempotent_and_conflicting_content_is_error(
    tp_accepted: CoordinatorState,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    one = dca_fill(tp_accepted)
    leg = round_of(one.state).dcas[0]
    assert leg.order is not None
    fill = leg.fills[0]
    retry = coordinator.on_fill(one.state, fill, leg.order)
    assert retry.state == one.state and not retry.cancel_requests and not retry.order_intents
    with pytest.raises(ValueError, match="FillId"):
        coordinator.on_fill(one.state, replace(fill, price=Price(Decimal("1"))), leg.order)
    assert round_of(one.state).dcas[0].fills == (fill,)


@pytest.mark.parametrize("field", ["round", "symbol", "side", "quantity", "reduce_only"])
def test_alien_or_altered_order_is_rejected(initial: CoordinationResult, field: str) -> None:
    intent = round_of(initial.state).dcas[0].intent
    if field == "round":
        bad = replace(intent, round_id=RoundId(UUID(int=77)))
    elif field == "symbol":
        bad = replace(intent, symbol=replace(intent.symbol, value="OTHERUSDC"))
    elif field == "side":
        bad = replace(intent, side=Side.SELL)
    elif field == "quantity":
        bad = replace(intent, quantity=Quantity(Decimal("999")))
    else:
        bad = replace(intent, reduce_only=True)
    order = order_for(bad, 301, opened_at(initial.state))
    with pytest.raises(ValueError):
        StrategyCoordinator(AltBEngine()).on_order(initial.state, order)


def test_overfill_is_retained_and_blocks_new_tp(tp_accepted: CoordinatorState) -> None:
    management = round_of(tp_accepted)
    leg = management.dcas[0]
    order = order_for(leg.intent, 301, opened_at(tp_accepted) + timedelta(seconds=1))
    fill = fill_for(order, 2001, Decimal("999"), "98")
    result = StrategyCoordinator(AltBEngine()).on_fill(tp_accepted, fill, order)
    assert round_of(result.state).dcas[0].fills == (fill,)
    base = state_of(result.state).base
    assert base is not None
    assert round_of(result.state).block_reason(base) == "execution_reconciliation_required"
    assert not result.order_intents


def test_no_second_order_id_for_same_level(tp_accepted: CoordinatorState) -> None:
    one = dca_fill(tp_accepted)
    order = round_of(one.state).dcas[0].order
    assert order is not None
    with pytest.raises(ValueError):
        StrategyCoordinator(AltBEngine()).on_order(
            one.state, replace(order, order_id=OrderId(UUID(int=999)))
        )


def test_ohlc_touching_both_tp_and_dca_does_not_determine_execution(
    tp_accepted: CoordinatorState, history: tuple[Candle, ...]
) -> None:
    at = opened_at(tp_accepted).replace(second=0) + timedelta(minutes=1)
    candle = replace(
        history[-1],
        open_time=at,
        close_time=at + timedelta(minutes=1),
        high=Price(Decimal("1000")),
        low=Price(Decimal("1")),
    )
    result = StrategyCoordinator(AltBEngine()).on_candle(tp_accepted, candle)
    assert round_of(result.state) == round_of(tp_accepted)
    assert not result.order_intents and not result.cancel_requests


def test_old_quote_does_not_apply_future_state(initial: CoordinationResult) -> None:
    logical = state_of(initial.state)
    assert logical.base is not None
    quote = replace(
        logical.base.reference_quote, timestamp=opened_at(initial.state) - timedelta(seconds=1)
    )
    with pytest.raises(ValueError, match="cursor causal"):
        StrategyCoordinator(AltBEngine()).on_quote(initial.state, quote)


def test_round_snapshot_and_cancellation_requests_are_frozen(initial: CoordinationResult) -> None:
    management = round_of(initial.state)
    cancellation = OrderCancelRequest(management.take_profits[0].intent, opened_at(initial.state))
    items: tuple[AltBRoundState | OrderCancelRequest | OrderLeg, ...] = (
        management,
        *management.legs,
        cancellation,
    )
    for item in items:
        for field in fields(item):
            with pytest.raises(FrozenInstanceError):
                setattr(item, field.name, None)
    with pytest.raises(TypeError):
        replace(management, dcas=cast(tuple[OrderLeg, ...], []))


def test_domain_has_no_io_random_clock_or_exchange_execution() -> None:
    root = Path(__file__).resolve().parents[4] / "src/trading_engine/strategy/alt_b/events"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                pytest.fail("Dependencias deben ser explícitas y puras")
            if isinstance(node, ast.ImportFrom):
                assert node.module is not None
                assert node.module in {
                    "dataclasses",
                    "datetime",
                    "decimal",
                    "typing",
                } or node.module.startswith(
                    ("trading_engine.models.", "trading_engine.strategy.alt_b.")
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "now",
                    "utcnow",
                    "sleep",
                    "uuid4",
                    "connect",
                    "submit",
                    "poll",
                }


def test_two_partial_dca_fills_and_late_order_delivery_are_reconciled(
    tp_accepted: CoordinatorState,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    leg = round_of(tp_accepted).dcas[0]
    at = opened_at(tp_accepted) + timedelta(seconds=3)
    report = order_for(leg.intent, 301, at, OrderStatus.PARTIALLY_FILLED, Decimal("0.5"))
    first = fill_for(report, 2101, Decimal("0.2"), "98", at - timedelta(seconds=2))
    second = fill_for(report, 2102, Decimal("0.3"), "97", at - timedelta(seconds=1))
    one = coordinator.on_order(tp_accepted, report)
    assert not one.order_intents and round_of(one.state).dcas[0].reconciliation_required
    two = coordinator.on_fill(one.state, second, report)
    three = coordinator.on_fill(two.state, first, report)
    assert round_of(three.state).dcas[0].executed_quantity == Decimal("0.5")
    assert not round_of(three.state).dcas[0].reconciliation_required
    assert state_of(three.state).last_event_at == at
    reverse = coordinator.on_fill(
        coordinator.on_fill(one.state, first, report).state, second, report
    )
    assert reverse.state == three.state
    reverse_base = state_of(reverse.state).base
    base = state_of(three.state).base
    assert reverse_base is not None and base is not None
    assert round_of(reverse.state).tp_target(reverse_base) == round_of(three.state).tp_target(base)


def test_all_seven_effective_dca_fills_contribute_to_average(tp_accepted: CoordinatorState) -> None:
    state = tp_accepted
    for level in range(1, 8):
        result = dca_fill(state, level, "0.25", str(100 - level))
        assert not result.order_intents
        state = result.state
    management = round_of(state)
    base = state_of(state).base
    assert base is not None
    with localcontext() as ctx:
        ctx.prec = 50
        qty = base.executed_quantity + Decimal("1.75")
        value = base.executed_quantity * 100 + sum(
            (Decimal("0.25") * (100 - k) for k in range(1, 8)), Decimal("0")
        )
        expected = value / qty
    assert management.quantity_open(base) == qty
    assert management.average_entry_price(base) == Price(expected)
    assert management.p0 == Price(Decimal("100"))
    assert len(management.take_profits) == 1 and len(management.cancel_requests) == 1
    replaced = cancel_tp(state)
    assert len(replaced.order_intents) == 1 and replaced.order_intents[0].quantity.value == qty


def test_multiple_goal_changes_while_cancel_pending_emit_only_latest_tp(
    tp_accepted: CoordinatorState,
) -> None:
    one = dca_fill(tp_accepted, 1)
    two = dca_fill(one.state, 2, price="96")
    assert len(one.cancel_requests) == 1 and not two.cancel_requests and not two.order_intents
    old = round_of(two.state).take_profits[0]
    assert old.order is not None and old.order.status is OrderStatus.OPEN
    replaced = cancel_tp(two.state)
    management = round_of(replaced.state)
    base = state_of(replaced.state).base
    assert base is not None
    assert len(replaced.order_intents) == 1
    assert replaced.order_intents[0].limit_price == management.tp_target(base)
    assert replaced.order_intents[0].quantity.value == management.quantity_open(base)


def test_exit_after_replacement_is_retained_and_cancels_new_tp_fail_closed(
    tp_accepted: CoordinatorState,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    changed = dca_fill(tp_accepted)
    replacement = cancel_tp(changed.state)
    previous = round_of(replacement.state).take_profits[0]
    assert previous.order is not None
    late = fill_for(previous.order, 4201, Decimal("0.1"), "101")
    result = coordinator.on_fill(replacement.state, late, previous.order)
    management = round_of(result.state)
    assert late in management.exit_fills and len(management.take_profits) == 2
    assert not result.order_intents
    assert management.take_profits[-1].intent in tuple(r.intent for r in result.cancel_requests)
    assert management.take_profits[0].reconciliation_required


def test_dca_fill_during_closing_is_kept_and_new_exit_waits_for_entry_resolution(
    tp_accepted: CoordinatorState,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    closing = close_exposure(tp_accepted)
    late_entry = dca_fill(closing.state)
    assert not late_entry.order_intents and round_of(late_entry.state).closing
    resolved = settle_dcas(late_entry.state)
    management = round_of(resolved)
    base = state_of(resolved).base
    assert base is not None
    assert management.quantity_open(base) == Decimal("0.25")
    assert len(management.take_profits) == 2
    assert management.take_profits[-1].intent.quantity.value == Decimal("0.25")
    assert management.closed_at is None
    assert coordinator.on_order(resolved, management.dcas[-1].order).state == resolved  # type: ignore[arg-type]


def test_tp_overexit_does_not_create_short_or_close_round(tp_accepted: CoordinatorState) -> None:
    leg = round_of(tp_accepted).take_profits[0]
    assert leg.order is not None
    fill = fill_for(leg.order, 4200, Decimal("999"), "101")
    result = StrategyCoordinator(AltBEngine()).on_fill(tp_accepted, fill, leg.order)
    management = round_of(result.state)
    base = state_of(result.state).base
    assert base is not None
    assert management.quantity_open(base) < 0 and management.closed_at is None
    assert fill in management.exit_fills and not result.order_intents
    assert all(r.intent.side is Side.BUY or r.intent.reduce_only for r in result.cancel_requests)


def test_order_contradiction_is_sticky_even_after_matching_fill(
    tp_accepted: CoordinatorState,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    first = dca_fill(tp_accepted)
    leg = round_of(first.state).dcas[0]
    assert leg.order is not None
    regression = replace(
        leg.order,
        status=OrderStatus.OPEN,
        filled_quantity=Decimal("0"),
        updated_at=leg.order.updated_at + timedelta(seconds=1),
    )
    result = coordinator.on_order(first.state, regression)
    assert round_of(result.state).dcas[0].order_conflict
    assert not result.order_intents
    resumed = coordinator.on_entry_control(result.state, True, opened_at(result.state))
    assert round_of(resumed.state).dcas[0].order_conflict


def test_fill_id_cannot_be_reused_in_another_level(tp_accepted: CoordinatorState) -> None:
    first = dca_fill(tp_accepted)
    management = round_of(first.state)
    second = order_for(
        management.dcas[1].intent,
        302,
        opened_at(first.state) + timedelta(seconds=1),
        OrderStatus.PARTIALLY_FILLED,
        Decimal("0.25"),
    )
    reused = fill_for(second, 2001, Decimal("0.25"), "97")
    with pytest.raises(ValueError, match="FillId"):
        StrategyCoordinator(AltBEngine()).on_fill(first.state, reused, second)


def test_reported_full_tp_without_fills_never_closes(tp_accepted: CoordinatorState) -> None:
    intent = round_of(tp_accepted).take_profits[0].intent
    reported = order_for(
        intent,
        200,
        opened_at(tp_accepted) + timedelta(seconds=1),
        OrderStatus.FILLED,
        intent.quantity.value,
    )
    result = StrategyCoordinator(AltBEngine()).on_order(tp_accepted, reported)
    management = round_of(result.state)
    base = state_of(result.state).base
    assert base is not None
    assert management.quantity_open(base) == base.executed_quantity
    assert management.closed_at is None and not management.exit_fills
    assert not result.order_intents


def test_rejected_tp_is_fail_closed_without_unbounded_auto_retry(
    tp_accepted: CoordinatorState,
) -> None:
    tp = round_of(tp_accepted).take_profits[0]
    assert tp.order is not None
    order = replace(
        tp.order, status=OrderStatus.REJECTED, updated_at=tp.order.updated_at + timedelta(seconds=1)
    )
    result = StrategyCoordinator(AltBEngine()).on_order(tp_accepted, order)
    assert not result.order_intents
    base = state_of(result.state).base
    assert base is not None
    assert round_of(result.state).block_reason(base) == "tp_rejected_requires_reconciliation"


def test_closed_history_duplicates_are_idempotent_and_late_new_fills_quarantine_account(
    tp_accepted: CoordinatorState,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    closed = settle_dcas(close_exposure(tp_accepted).state)
    record = state_of(closed).round_history[0]
    assert record.base.order is not None
    duplicate = coordinator.on_fill(closed, record.base.fills[0], record.base.order)
    assert duplicate.state == closed and not duplicate.order_intents
    canceled = record.round_state.dcas[0].order
    assert canceled is not None
    late = fill_for(canceled, 9900, Decimal("0.1"), "98", canceled.updated_at)
    unexpected = coordinator.on_fill(closed, late, canceled)
    stored = state_of(unexpected.state).round_history[0]
    assert late in stored.round_state.entry_fills(stored.base)
    assert stored.round_state.reconciliation_reason == "late_execution_after_close"
    assert not stored.round_state.can_close(stored.base)
    resumed = coordinator.on_entry_control(unexpected.state, True, opened_at(unexpected.state))
    assert state_of(resumed.state).block_reason == "historical_execution_reconciliation"
    assert not resumed.signals and not resumed.order_intents


def test_new_signal_after_proven_close_keeps_previous_round_evidence(
    tp_accepted: CoordinatorState, history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    state = settle_dcas(close_exposure(tp_accepted).state)
    record = state_of(state).round_history[0]
    # Proveer nuevamente 26h de minutos sintéticos causales, sin buscar datos.
    shift = timedelta(days=2)
    new_history = tuple(
        replace(c, open_time=c.open_time + shift, close_time=c.close_time + shift) for c in history
    )
    state = coordinator.on_candle(
        state, new_history[0]
    ).state  # Gap invalida únicamente historial de mercado.
    for candle in new_history[1:]:
        result = coordinator.on_candle(state, candle)
        state = result.state
    assert len(result.signals) == 1 and state_of(state).pending_entry is not None
    assert state_of(state).round_history == (record,)
    assert result.signals[0].timestamp == checkpoint + shift


def test_reconstruction_rejects_changed_p0_missing_level_or_budget(
    initial: CoordinationResult,
) -> None:
    logical = state_of(initial.state)
    management = round_of(initial.state)
    for changed in (
        replace(management, p0=Price(Decimal("99"))),
        replace(management, config=AltBConfig(order_budget=Decimal("2400"))),
    ):
        with pytest.raises(ValueError):
            replace(logical, round_state=changed)
    with pytest.raises(ValueError, match="siete"):
        replace(management, dcas=management.dcas[:-1])


def test_equivalent_base_fact_delivery_produces_identical_round_and_outputs(
    base_pending: CoordinatorState, initial: CoordinationResult
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    base = state_of(initial.state).base
    assert base is not None and base.order is not None
    reported = coordinator.on_order(base_pending, base.order)
    assert not reported.order_intents
    observed = coordinator.on_fill(reported.state, base.fills[0], base.order)
    assert observed == initial


def test_dca_level_ids_have_frozen_golden_vectors(initial: CoordinationResult) -> None:
    # Los UUID se congelan aquí tras verificar la selección causal del contrato.
    identifiers = tuple(str(leg.intent.intent_id) for leg in round_of(initial.state).dcas)
    assert identifiers == (
        "401cb4fb-2375-5acd-ab81-e4182953def0",
        "f8c7f4f9-9392-5c0c-8949-c8703aef3771",
        "58b36c78-e4bc-5458-b2d4-1bad96992cc7",
        "e7daa9e8-7f2f-5c89-843d-6d00bb2387c9",
        "65a47872-2f18-5f1e-b841-79f8bc52fc3f",
        "ddf06156-af5e-5d4f-ae07-fff26f2fb5f8",
        "4f3a3d91-fc69-5e6d-a8e6-b03b08ea351d",
    )
    assert all(leg.intent.intent_id.version == 5 for leg in round_of(initial.state).dcas)
    # Reprocesar Order/Fill ya consumidos no genera otra revisión ni otro nivel.
    base = state_of(initial.state).base
    assert base is not None and base.order is not None
    retry = StrategyCoordinator(AltBEngine()).on_fill(initial.state, base.fills[0], base.order)
    assert tuple(str(leg.intent.intent_id) for leg in round_of(retry.state).dcas) == identifiers


def test_pending_cancel_survives_reconstruction_and_retry(tp_accepted: CoordinatorState) -> None:
    result = dca_fill(tp_accepted)
    management = round_of(result.state)
    restored = replace(
        result.state,
        strategy_state=replace(state_of(result.state), round_state=replace(management)),
    )
    assert restored == result.state and len(management.pending_cancellations()) == 1
    order = management.dcas[0].order
    assert order is not None
    retry = StrategyCoordinator(AltBEngine()).on_order(restored, order)
    assert retry.state == restored and not retry.cancel_requests and not retry.order_intents


def test_round_context_incompatible_blocks_new_orders_but_retains_fill(
    tp_accepted: CoordinatorState,
) -> None:
    from trading_engine.models.enums import RoundStatus
    from trading_engine.models.rounds import Round

    management = round_of(tp_accepted)
    at = opened_at(tp_accepted) + timedelta(seconds=1)
    order = order_for(
        management.dcas[0].intent, 301, at, OrderStatus.PARTIALLY_FILLED, Decimal("0.25")
    )
    fill = fill_for(order, 2001, Decimal("0.25"), "98")
    alien = Round(RoundId(UUID(int=999)), order.symbol, RoundStatus.PENDING_ENTRY, at)
    result = StrategyCoordinator(AltBEngine()).on_fill(tp_accepted, fill, order, round_state=alien)
    assert round_of(result.state).dcas[0].fills == (fill,)
    assert state_of(result.state).block_reason == "round_context_incompatible"
    assert not result.order_intents and result.cancel_requests


def test_restored_snapshot_cannot_claim_two_unconfirmed_tps(tp_accepted: CoordinatorState) -> None:
    changed = dca_fill(tp_accepted)
    replacement = cancel_tp(changed.state)
    management = round_of(replacement.state)
    old, current = management.take_profits
    with pytest.raises(ValueError, match="TP potencialmente"):
        replace(management, take_profits=(replace(old, order=None), current))


def test_confirmed_close_status_changes_to_reconciliation_after_late_fill(
    tp_accepted: CoordinatorState,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    closed = settle_dcas(close_exposure(tp_accepted).state)
    record = state_of(closed).round_history[0]
    assert record.round_state.status(record.base) == "CLOSED"
    order = record.round_state.dcas[0].order
    assert order is not None
    fill = fill_for(order, 9901, Decimal("0.1"), "98")
    late = coordinator.on_fill(closed, fill, order)
    updated = state_of(late.state).round_history[0]
    assert updated.round_state.status(updated.base) == "RECONCILIATION"
    assert updated.round_state.closed_at == record.round_state.closed_at


def test_incompatible_provider_order_id_cannot_be_shared_between_levels(
    tp_accepted: CoordinatorState,
) -> None:
    first = dca_fill(tp_accepted)
    management = round_of(first.state)
    first_order = management.dcas[0].order
    assert first_order is not None
    second = order_for(management.dcas[1].intent, first_order.order_id.int, opened_at(first.state))
    with pytest.raises(ValueError, match="OrderId"):
        StrategyCoordinator(AltBEngine()).on_order(first.state, second)


def future_pending(
    closed: CoordinatorState, history: tuple[Candle, ...], checkpoint: datetime
) -> CoordinatorState:
    shift = timedelta(days=2)
    future_history = tuple(
        replace(c, open_time=c.open_time + shift, close_time=c.close_time + shift) for c in history
    )
    signal, action = entry_identity(AltBConfig(), checkpoint + shift)
    logical = replace(
        state_of(closed),
        market=AltBMarketState(future_history),
        allow_entries=True,
        pending_entry=PendingEntry(signal, action),
        last_event_at=signal.timestamp,
    )
    return CoordinatorState((action,), logical)


@pytest.mark.parametrize("phase", ["waiting", "base_intent", "open_round"])
def test_historical_late_fill_quarantines_new_round_without_reemitting_cancel(
    tp_accepted: CoordinatorState, history: tuple[Candle, ...], checkpoint: datetime, phase: str
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    closed = settle_dcas(close_exposure(tp_accepted).state)
    state = future_pending(closed, history, checkpoint)
    if phase != "waiting":
        action = state.pending_actions[0]
        q = Quote(action.symbol, action.not_before, Price(Decimal("99")), Price(Decimal("100")))
        state = coordinator.on_scheduled_action(state, action, q).state
    if phase == "open_round":
        base = state_of(state).base
        assert base is not None
        at = base.intent.created_at + timedelta(seconds=1)
        order = order_for(base.intent, 999, at, OrderStatus.FILLED, base.intent.quantity.value)
        fill = fill_for(order, 9990, order.filled_quantity, "100")
        state = coordinator.on_fill(state, fill, order).state
    previous = state_of(state).round_history[0]
    old_order = previous.round_state.dcas[0].order
    assert old_order is not None
    old_fill = fill_for(old_order, 9902, Decimal("0.1"), "98")
    result = coordinator.on_fill(state, old_fill, old_order)
    logical = state_of(result.state)
    assert old_fill in logical.round_history[0].round_state.entry_fills(previous.base)
    assert logical.block_reason == "historical_execution_reconciliation"
    assert not result.order_intents and not result.signals and not result.state.pending_actions
    if phase == "waiting":
        assert logical.pending_entry is None and not result.cancel_requests
    elif phase == "base_intent":
        assert logical.base_cancel_request is not None
        assert result.cancel_requests == (logical.base_cancel_request,)
        with pytest.raises(ValueError, match="cursor causal"):
            replace(
                logical,
                base_cancel_request=replace(
                    logical.base_cancel_request,
                    created_at=opened_at(result.state) + timedelta(seconds=1),
                ),
            )
    else:
        assert len(result.cancel_requests) == 8
    retry = coordinator.on_fill(result.state, old_fill, old_order)
    assert retry.state == result.state and not retry.cancel_requests and not retry.order_intents
