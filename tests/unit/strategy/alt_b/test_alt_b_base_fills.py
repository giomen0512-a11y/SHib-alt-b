"""Base offline: cantidades teóricas, evidencia parcial y reconciliación causal."""

import ast
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from trading_engine.models.account import Position
from trading_engine.models.enums import OrderStatus, OrderType, PositionSide, RoundStatus, Side
from trading_engine.models.identity import make_intent_id
from trading_engine.models.ids import ActionId, FillId, IntentId, OrderId, RoundId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order
from trading_engine.models.rounds import Round
from trading_engine.models.values import Price, Quantity, Symbol
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinationResult, CoordinatorState
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.base import BaseState
from trading_engine.strategy.alt_b.events.engine import AltBEngine
from trading_engine.strategy.alt_b.events.state import (
    AltBStrategyState,
    PendingEntry,
    entry_identity,
)
from trading_engine.strategy.alt_b.state_market import AltBMarketState


def snapshot(state: CoordinatorState) -> AltBStrategyState:
    assert isinstance(state.strategy_state, AltBStrategyState)
    return state.strategy_state


def base_of(state: CoordinatorState) -> BaseState:
    base = snapshot(state).base
    assert base is not None
    return base


def quote(at: datetime, ask: str = "101") -> Quote:
    return Quote(Symbol("1000SHIBUSDC"), at, Price(Decimal("100")), Price(Decimal(ask)))


@pytest.fixture
def pending(history: tuple[Candle, ...], checkpoint: datetime) -> CoordinatorState:
    signal, action = entry_identity(AltBConfig(), checkpoint)
    return CoordinatorState(
        (action,),
        AltBStrategyState(
            market=AltBMarketState(history),
            allow_entries=True,
            pending_entry=PendingEntry(signal, action),
            last_event_at=checkpoint,
        ),
    )


@pytest.fixture
def emitted(pending: CoordinatorState) -> CoordinationResult:
    action = pending.pending_actions[0]
    return StrategyCoordinator(AltBEngine()).on_scheduled_action(
        pending, action, quote(action.not_before)
    )


def order_for(
    state: CoordinatorState,
    status: OrderStatus = OrderStatus.OPEN,
    filled: Decimal = Decimal("0"),
    seconds: int = 1,
) -> Order:
    intent = base_of(state).intent
    return Order(
        OrderId(UUID(int=200)),
        intent,
        status,
        filled,
        intent.created_at,
        intent.created_at + timedelta(seconds=seconds),
    )


def fill_for(order: Order, number: int, qty: Decimal, price: str = "103", seconds: int = 1) -> Fill:
    return Fill(
        FillId(UUID(int=number)),
        order.order_id,
        order.intent.created_at + timedelta(seconds=seconds),
        Price(Decimal(price)),
        Quantity(qty),
    )


@pytest.mark.parametrize("seconds", [0, 119])
def test_base_cannot_be_emitted_before_delay(pending: CoordinatorState, seconds: int) -> None:
    action = pending.pending_actions[0]
    with pytest.raises(ValueError, match="not_before"):
        AltBEngine().on_scheduled_action(
            action,
            quote(action.created_at + timedelta(seconds=seconds)),
            None,
            None,
            strategy_state=pending.strategy_state,
        )
    assert snapshot(pending).base is None and len(pending.pending_actions) == 1


@pytest.mark.parametrize("seconds", [120, 121, 180])
def test_authorized_due_action_consumes_reservation_only_once(
    pending: CoordinatorState, seconds: int
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    action = pending.pending_actions[0]
    q = quote(action.created_at + timedelta(seconds=seconds))
    one = coordinator.on_scheduled_action(pending, action, q)
    assert len(one.order_intents) == 1 and not one.signals and not one.state.pending_actions
    base = base_of(one.state)
    assert base.intent.side is Side.BUY and base.intent.order_type is OrderType.MARKET
    assert base.intent.reduce_only is False and base.intent.limit_price is None
    assert base.action == action and base.intent.round_id == action.round_id
    assert base.order_pending and base.order is None and not base.fills
    assert snapshot(one.state).pending_entry is None
    retry = coordinator.on_scheduled_action(one.state, action, q)
    assert not retry.order_intents and retry.state == one.state
    later = coordinator.on_scheduled_action(
        one.state, action, replace(q, timestamp=q.timestamp + timedelta(seconds=1))
    )
    assert not later.order_intents and base_of(later.state) == base


def test_theoretical_sizing_is_canonical_at_50_digits_and_4x_once(
    pending: CoordinatorState,
) -> None:
    action = pending.pending_actions[0]
    with localcontext() as ctx:
        ctx.prec = 50
        margin = Decimal("1200") * Decimal("59") / Decimal("1704.882944")
        expected = margin * Decimal("4") / Decimal("101")
    with localcontext() as hostile:
        hostile.prec = 6
        hostile.rounding = ROUND_UP
        result = StrategyCoordinator(AltBEngine()).on_scheduled_action(
            pending, action, quote(action.not_before)
        )
        assert hostile.prec == 6 and hostile.rounding == ROUND_UP
    assert result.order_intents[0].quantity.value == expected
    assert len(expected.as_tuple().digits) == 50
    assert base_of(result.state).executed_quantity == 0


def test_sizing_uses_configured_budget_and_rejects_restore_under_different_config(
    pending: CoordinatorState,
) -> None:
    action = pending.pending_actions[0]
    engine = AltBEngine(AltBConfig(order_budget=Decimal("2400")))
    result = StrategyCoordinator(engine).on_scheduled_action(
        pending, action, quote(action.not_before)
    )
    with localcontext() as ctx:
        ctx.prec = 50
        expected = Decimal("2400") * 59 / Decimal("1704.882944") * 4 / 101
    assert result.order_intents[0].quantity.value == expected
    with pytest.raises(ValueError, match="configuración"):
        StrategyCoordinator(AltBEngine()).on_quote(result.state, quote(action.not_before))


def test_same_snapshot_has_same_intent_uuid_v5_and_no_hidden_state(
    pending: CoordinatorState, emitted: CoordinationResult
) -> None:
    engine = AltBEngine()
    action = pending.pending_actions[0]
    again = StrategyCoordinator(engine).on_scheduled_action(
        replace(pending, strategy_state=replace(snapshot(pending))),
        action,
        quote(action.not_before),
    )
    assert again == emitted and again.order_intents[0].intent_id.version == 5
    assert str(again.order_intents[0].intent_id) == "3e8dc55a-bdbd-527b-84df-0b01fdc28862"
    assert tuple(f.name for f in fields(engine)) == ("config",)


@pytest.mark.parametrize("control", ["pause", "invalidate"])
def test_pause_and_invalidation_before_entry_prevent_base(
    pending: CoordinatorState, control: str
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    action = pending.pending_actions[0]
    at = action.created_at + timedelta(seconds=30)
    stopped = (
        coordinator.on_entry_control(pending, False, at)
        if control == "pause"
        else coordinator.invalidate_action(pending, action.action_id, at)
    )
    retry = coordinator.on_scheduled_action(stopped.state, action, quote(action.not_before))
    assert not retry.order_intents and snapshot(retry.state).base is None
    assert snapshot(retry.state).pending_entry is None


@pytest.mark.parametrize("context", ["long", "short", "round", "future_round", "same_open_round"])
def test_incompatible_position_or_round_cannot_produce_base(
    pending: CoordinatorState, context: str
) -> None:
    action = pending.pending_actions[0]
    position = None
    round_state = None
    if context in ("long", "short"):
        position = Position(
            action.symbol,
            PositionSide.LONG if context == "long" else PositionSide.SHORT,
            Quantity(Decimal("1")),
            Price(Decimal("100")),
            action.not_before,
        )
    else:
        at = (
            action.not_before + timedelta(seconds=1)
            if context == "future_round"
            else action.created_at
        )
        round_state = Round(
            action.round_id if context == "same_open_round" else RoundId(UUID(int=4)),
            action.symbol,
            RoundStatus.OPEN,
            at,
            at,
        )
    result = StrategyCoordinator(AltBEngine()).on_scheduled_action(
        pending, action, quote(action.not_before), position, round_state
    )
    assert not result.order_intents and snapshot(result.state).base is None
    assert not result.state.pending_actions


def test_own_pending_round_is_compatible_with_reservation(pending: CoordinatorState) -> None:
    action = pending.pending_actions[0]
    own = Round(action.round_id, action.symbol, RoundStatus.PENDING_ENTRY, action.created_at)
    result = StrategyCoordinator(AltBEngine()).on_scheduled_action(
        pending, action, quote(action.not_before), round_state=own
    )
    assert len(result.order_intents) == 1


@pytest.mark.parametrize("field", ["id", "kind", "round", "delay"])
def test_invalid_action_identity_never_produces_intent(
    pending: CoordinatorState, field: str
) -> None:
    action = pending.pending_actions[0]
    if field == "id":
        invalid = replace(action, action_id=ActionId(UUID(int=99)))
    elif field == "kind":
        invalid = replace(action, kind="other")
    elif field == "round":
        invalid = replace(action, round_id=RoundId(UUID(int=99)))
    else:
        invalid = replace(action, not_before=action.not_before + timedelta(seconds=1))
    result = StrategyCoordinator(AltBEngine()).on_scheduled_action(
        pending, invalid, quote(invalid.not_before)
    )
    assert not result.order_intents and snapshot(result.state).base is None
    assert not result.state.pending_actions


def test_quote_is_neither_fill_nor_p0(emitted: CoordinationResult) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    base = base_of(emitted.state)
    new_quote = quote(base.intent.created_at + timedelta(seconds=1), "500")
    result = coordinator.on_quote(emitted.state, new_quote)
    assert base_of(result.state) == base
    assert base.executed_quantity == 0 and base.average_fill_price is None and base.p0 is None
    assert not result.order_intents


@pytest.mark.parametrize("status", [OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.FILLED])
def test_order_snapshot_does_not_manufacture_fills(
    emitted: CoordinationResult, status: OrderStatus
) -> None:
    base = base_of(emitted.state)
    qty = base.intent.quantity.value if status is OrderStatus.FILLED else Decimal("0")
    result = StrategyCoordinator(AltBEngine()).on_order(
        emitted.state, order_for(emitted.state, status, qty)
    )
    observed = base_of(result.state)
    assert observed.executed_quantity == 0 and not observed.fills and observed.p0 is None
    assert observed.reconciliation_required is (status is OrderStatus.FILLED)
    assert not result.order_intents


def test_complete_fill_defines_p0_only_when_base_resolved(emitted: CoordinationResult) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    initial = base_of(emitted.state)
    order = order_for(emitted.state, OrderStatus.FILLED, initial.intent.quantity.value)
    fill = fill_for(order, 1, order.filled_quantity, "104")
    result = coordinator.on_fill(emitted.state, fill, order)
    base = base_of(result.state)
    assert base.fills == (fill,) and base.executed_quantity == fill.quantity.value
    assert base.average_fill_price == base.p0 == fill.price
    assert not base.order_pending and not base.reconciliation_required
    assert base.block_reason == "base_confirmed"
    assert len(result.order_intents) == 8
    assert all(i.side is Side.BUY for i in result.order_intents[:7])
    assert result.order_intents[-1].side is Side.SELL and result.order_intents[-1].reduce_only
    management = snapshot(result.state).round_state
    assert management is not None and management.p0 == fill.price


def test_multiple_partial_fills_and_vwap_use_only_effective_quantities(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state, OrderStatus.PARTIALLY_FILLED, Decimal("0.75"), seconds=3)
    first = fill_for(order, 1, Decimal("0.25"), "102", seconds=1)
    second = fill_for(order, 2, Decimal("0.5"), "108", seconds=2)
    one = coordinator.on_fill(emitted.state, first, order)
    assert base_of(one.state).executed_quantity == Decimal("0.25")
    assert base_of(one.state).reconciliation_required
    two = coordinator.on_fill(one.state, second, order)
    base = base_of(two.state)
    assert base.executed_quantity == Decimal("0.75")
    assert base.average_fill_price == Price(Decimal("106")) and base.p0 is None
    assert base.order_pending and not base.reconciliation_required
    assert not two.order_intents
    reverse = coordinator.on_fill(
        coordinator.on_fill(emitted.state, second, order).state, first, order
    )
    assert reverse.state == two.state  # Canonicalización no depende de entrega.


def test_order_report_then_late_fills_reconciles_without_watermark_rollback(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    total = base_of(emitted.state).intent.quantity.value
    order = order_for(emitted.state, OrderStatus.FILLED, total, seconds=5)
    waiting = coordinator.on_order(emitted.state, order)
    assert base_of(waiting.state).p0 is None and base_of(waiting.state).reconciliation_required
    with localcontext() as ctx:
        ctx.prec = 50
        remainder = total - Decimal("0.5")
    first = fill_for(order, 1, Decimal("0.5"), "102", seconds=1)
    second = fill_for(order, 2, remainder, "105", seconds=2)
    state = coordinator.on_fill(waiting.state, second, order).state
    result = coordinator.on_fill(state, first, order)
    base = base_of(result.state)
    with localcontext() as ctx:
        ctx.prec = 50
        expected = (Decimal("0.5") * 102 + remainder * 105) / total
    assert base.executed_quantity == total and base.p0 == Price(expected)
    assert not base.reconciliation_required
    assert snapshot(result.state).last_event_at == order.updated_at


def test_fill_before_reported_quantity_never_substitutes_requested_quantity(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    open_order = order_for(emitted.state)
    fill = fill_for(open_order, 1, Decimal("0.5"))
    observed = coordinator.on_fill(emitted.state, fill, open_order)
    base = base_of(observed.state)
    assert base.executed_quantity == Decimal("0.5") and base.reconciliation_required
    assert base.p0 is None and base.intent.quantity.value != base.executed_quantity
    report = replace(
        open_order,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=Decimal("0.5"),
        updated_at=open_order.updated_at + timedelta(seconds=1),
    )
    resolved = coordinator.on_order(observed.state, report)
    assert not base_of(resolved.state).reconciliation_required
    assert base_of(resolved.state).executed_quantity == Decimal("0.5")


def test_duplicate_fill_id_does_not_count_twice_and_conflict_is_rejected(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state, OrderStatus.PARTIALLY_FILLED, Decimal("0.5"))
    fill = fill_for(order, 1, Decimal("0.5"))
    one = coordinator.on_fill(emitted.state, fill, order)
    assert coordinator.on_fill(one.state, fill, order) == one
    for conflict in (
        replace(fill, price=Price(Decimal("999"))),
        replace(fill, quantity=Quantity(Decimal("0.25"))),
        replace(fill, timestamp=fill.timestamp + timedelta(seconds=1)),
    ):
        with pytest.raises(ValueError, match="FillId"):
            coordinator.on_fill(one.state, conflict, order)
    assert base_of(one.state).executed_quantity == Decimal("0.5")
    assert base_of(one.state).fills == (fill,)


@pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.CANCELED])
def test_zero_fill_terminal_order_does_not_release_reservation(
    emitted: CoordinationResult, status: OrderStatus
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state, status)
    result = coordinator.on_order(emitted.state, order)
    base = base_of(result.state)
    assert base.executed_quantity == 0 and base.p0 is None and not base.order_pending
    assert not result.order_intents
    retried = coordinator.on_scheduled_action(result.state, base.action, quote(order.updated_at))
    assert not retried.order_intents and base_of(retried.state) == base


def test_partially_filled_then_canceled_is_blocked_and_preserves_evidence(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state, OrderStatus.PARTIALLY_FILLED, Decimal("0.5"))
    fill = fill_for(order, 1, Decimal("0.5"), "104")
    partial = coordinator.on_fill(emitted.state, fill, order)
    canceled = replace(
        order, status=OrderStatus.CANCELED, updated_at=order.updated_at + timedelta(seconds=1)
    )
    result = coordinator.on_order(partial.state, canceled)
    base = base_of(result.state)
    assert base.executed_quantity == Decimal("0.5") and base.fills == (fill,)
    assert base.average_fill_price == fill.price and base.p0 is None
    assert base.reconciliation_required and base.block_reason == "base_shortfill_reconciliation"
    paused = coordinator.on_entry_control(result.state, False, canceled.updated_at)
    resumed = coordinator.on_entry_control(paused.state, True, canceled.updated_at)
    assert base_of(paused.state) == base_of(resumed.state) == base
    assert not resumed.order_intents and not resumed.signals


def test_rejected_order_after_confirmed_fill_blocks_and_preserves_quantity(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state)
    fill = fill_for(order, 1, Decimal("0.5"))
    filled = coordinator.on_fill(emitted.state, fill, order)
    rejected = replace(
        order, status=OrderStatus.REJECTED, updated_at=order.updated_at + timedelta(seconds=1)
    )
    result = coordinator.on_order(filled.state, rejected)
    assert base_of(result.state).reconciliation_required
    assert base_of(result.state).fills == (fill,) and base_of(result.state).p0 is None


def test_late_fill_after_cancel_never_reopens_or_loses_evidence(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    canceled = order_for(emitted.state, OrderStatus.CANCELED, Decimal("0.5"), seconds=3)
    state = coordinator.on_order(emitted.state, canceled).state
    fill = fill_for(canceled, 1, Decimal("0.5"), seconds=1)
    result = coordinator.on_fill(state, fill, canceled)
    base = base_of(result.state)
    assert base.fills == (fill,) and base.block_reason == "base_shortfill_reconciliation"
    assert snapshot(result.state).last_event_at == canceled.updated_at


def test_compatible_old_order_snapshot_cannot_rollback_current_status(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    old = order_for(emitted.state)
    new = order_for(emitted.state, OrderStatus.CANCELED, seconds=3)
    current = coordinator.on_order(emitted.state, new)
    late = coordinator.on_order(current.state, old)
    assert late == current and base_of(late.state).order == new


@pytest.mark.parametrize("reverse", [False, True])
def test_order_quantity_regression_requires_sticky_reconciliation(
    emitted: CoordinationResult, reverse: bool
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    old = order_for(emitted.state, OrderStatus.PARTIALLY_FILLED, Decimal("0.5"), seconds=1)
    new = order_for(emitted.state, OrderStatus.PARTIALLY_FILLED, Decimal("0.25"), seconds=2)
    one, two = (new, old) if reverse else (old, new)
    result = coordinator.on_order(coordinator.on_order(emitted.state, one).state, two)
    assert base_of(result.state).order == new
    assert base_of(result.state).reconciliation_reason == "base_order_conflict"
    assert base_of(result.state).reconciliation_required


def test_terminal_order_cannot_silently_be_reopened(emitted: CoordinationResult) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    canceled = order_for(emitted.state, OrderStatus.CANCELED)
    opened = replace(
        canceled, status=OrderStatus.OPEN, updated_at=canceled.updated_at + timedelta(seconds=1)
    )
    result = coordinator.on_order(coordinator.on_order(emitted.state, canceled).state, opened)
    assert base_of(result.state).reconciliation_required
    assert base_of(result.state).reconciliation_reason == "base_order_conflict"


@pytest.mark.parametrize(
    "kind", ["intent", "symbol", "round", "order_id", "order_time", "same_time"]
)
def test_incorrect_order_identity_or_conflicting_snapshot_is_rejected(
    emitted: CoordinationResult, kind: str
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state)
    one = coordinator.on_order(emitted.state, order)
    if kind == "intent":
        invalid = replace(order, intent=replace(order.intent, intent_id=IntentId(UUID(int=98))))
    elif kind == "symbol":
        invalid = replace(order, intent=replace(order.intent, symbol=Symbol("OTHERUSDC")))
    elif kind == "round":
        invalid = replace(order, intent=replace(order.intent, round_id=RoundId(UUID(int=98))))
    elif kind == "order_id":
        invalid = replace(order, order_id=OrderId(UUID(int=98)))
    elif kind == "order_time":
        invalid = replace(order, created_at=order.updated_at)
    else:
        invalid = replace(order, status=OrderStatus.CANCELED)
    with pytest.raises(ValueError):
        coordinator.on_order(one.state, invalid)
    assert base_of(one.state).order == order and not base_of(one.state).fills


def test_fill_wrong_order_or_before_intent_is_rejected(emitted: CoordinationResult) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state)
    fill = fill_for(order, 1, Decimal("0.5"))
    for invalid in (
        replace(fill, order_id=OrderId(UUID(int=99))),
        replace(fill, timestamp=order.intent.created_at - timedelta(seconds=1)),
    ):
        with pytest.raises(ValueError):
            coordinator.on_fill(emitted.state, invalid, order)
    assert not base_of(emitted.state).fills


def test_overfill_keeps_evidence_and_blocks_instead_of_silently_clamping(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state)
    fill = fill_for(order, 1, Decimal("999"))
    result = coordinator.on_fill(emitted.state, fill, order)
    assert base_of(result.state).executed_quantity == Decimal("999")
    assert base_of(result.state).fills == (fill,)
    assert (
        base_of(result.state).block_reason == "base_overfill" and base_of(result.state).p0 is None
    )


def test_new_checkpoint_gap_pause_or_invalidation_cannot_discard_base(
    emitted: CoordinationResult, history: tuple[Candle, ...]
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    base = base_of(emitted.state)
    state = emitted.state
    # 15 minutos nuevos, primer minuto tras un gap; siguiente checkpoint sigue reservado.
    for i in range(15):
        opened = base.action.created_at + timedelta(minutes=15 + i)
        candle = replace(history[i], open_time=opened, close_time=opened + timedelta(minutes=1))
        result = coordinator.on_candle(state, candle)
        assert not result.signals and not result.order_intents and base_of(result.state) == base
        state = result.state
    at = snapshot(state).last_event_at
    assert at is not None
    paused = coordinator.on_entry_control(state, False, at)
    invalidated = coordinator.invalidate_action(paused.state, base.action.action_id, at)
    assert base_of(invalidated.state) == base
    assert not snapshot(invalidated.state).allow_entries


def test_snapshot_reconstruction_is_immutable_and_queue_cannot_requeue_consumed_action(
    emitted: CoordinationResult,
) -> None:
    base = base_of(emitted.state)
    reconstructed = replace(
        emitted.state, strategy_state=replace(snapshot(emitted.state), base=replace(base))
    )
    assert reconstructed == emitted.state
    for value in (base, base.intent, snapshot(emitted.state)):
        for field in fields(value):
            with pytest.raises(FrozenInstanceError):
                setattr(value, field.name, None)
    with pytest.raises(ValueError, match="scheduling"):
        replace(reconstructed, pending_actions=(base.action,))
    with pytest.raises(TypeError):
        replace(base, fills=cast(tuple[Fill, ...], []))
    order = order_for(emitted.state)
    fill = fill_for(order, 1, Decimal("0.5"))
    with pytest.raises(ValueError, match="duplicado"):
        replace(base, order=order, fills=(fill, fill))


def test_altered_theoretical_quantity_is_rejected_on_restore(emitted: CoordinationResult) -> None:
    base = base_of(emitted.state)
    changed = replace(base, intent=replace(base.intent, quantity=Quantity(Decimal("999"))))
    state = replace(emitted.state, strategy_state=replace(snapshot(emitted.state), base=changed))
    with pytest.raises(ValueError, match="configuración"):
        StrategyCoordinator(AltBEngine()).on_quote(state, base.reference_quote)


def test_base_module_has_no_io_clock_randomness_or_private_binance_dependencies() -> None:
    root = Path(__file__).resolve().parents[4] / "src/trading_engine/strategy/alt_b/events"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                pytest.fail("Solo dependencias puras explícitas")
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


@pytest.mark.parametrize("event", ["order", "fill"])
def test_unknown_execution_cannot_be_cleared_by_pause_resume(
    pending: CoordinatorState, emitted: CoordinationResult, event: str
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state)
    fill = fill_for(order, 1, Decimal("0.5"))
    unknown = (
        coordinator.on_order(pending, order)
        if event == "order"
        else coordinator.on_fill(pending, fill, order)
    )
    at = order.updated_at
    paused = coordinator.on_entry_control(unknown.state, False, at)
    resumed = coordinator.on_entry_control(paused.state, True, at)
    assert snapshot(resumed.state).block_reason == "execution_without_base_reservation"
    assert snapshot(resumed.state).pending_entry is None
    assert not resumed.signals and not resumed.order_intents
    q = coordinator.on_quote(resumed.state, quote(at))
    assert snapshot(q.state).block_reason == "execution_without_base_reservation"


def test_fills_still_processed_after_pause_with_existing_exposure(
    emitted: CoordinationResult,
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state, OrderStatus.PARTIALLY_FILLED, Decimal("0.5"), seconds=2)
    paused = coordinator.on_entry_control(emitted.state, False, order.updated_at)
    fill = fill_for(order, 1, Decimal("0.5"), seconds=1)
    result = coordinator.on_fill(paused.state, fill, order)
    assert not snapshot(result.state).allow_entries
    assert base_of(result.state).executed_quantity == Decimal("0.5")
    assert base_of(result.state).fills == (fill,) and not result.order_intents


def test_vwap_has_50_digit_context_independent_of_caller(emitted: CoordinationResult) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    order = order_for(emitted.state, OrderStatus.PARTIALLY_FILLED, Decimal("0.3"), seconds=2)
    first = fill_for(order, 1, Decimal("0.1"), "100", seconds=1)
    second = fill_for(order, 2, Decimal("0.2"), "103", seconds=2)
    with localcontext() as ctx:
        ctx.prec = 50
        expected = (Decimal("0.1") * 100 + Decimal("0.2") * 103) / Decimal("0.3")
    with localcontext() as ctx:
        ctx.prec = 5
        ctx.rounding = ROUND_UP
        one = coordinator.on_fill(emitted.state, first, order)
        two = coordinator.on_fill(one.state, second, order)
        assert base_of(two.state).average_fill_price == Price(expected)
        assert ctx.prec == 5 and ctx.rounding == ROUND_UP


def test_invalid_snapshot_does_not_forge_base_action_time(emitted: CoordinationResult) -> None:
    base = base_of(emitted.state)
    invalid = replace(base.action, action_id=ActionId(UUID(int=98)))
    # Rebuild the intent ID so that local identity is plausible; state must still reject the action.
    intent = replace(
        base.intent,
        intent_id=make_intent_id(
            invalid.round_id, "ALT_B_BASE", base.intent.created_at, str(invalid.action_id)
        ),
    )
    forged = replace(base, action=invalid, intent=intent)
    with pytest.raises(ValueError, match="Identidad"):
        replace(snapshot(emitted.state), base=forged)


def test_snapshot_from_previous_stage_cannot_execute_old_evaluated_entry(
    pending: CoordinatorState,
) -> None:
    p = snapshot(pending).pending_entry
    assert p is not None
    previous = replace(
        pending,
        strategy_state=replace(
            snapshot(pending),
            pending_entry=replace(p, evaluated_at=p.action.not_before),
            last_event_at=p.action.not_before,
        ),
    )
    result = StrategyCoordinator(AltBEngine()).on_scheduled_action(
        previous, p.action, quote(p.action.not_before)
    )
    assert not result.order_intents
    assert snapshot(result.state).block_reason == "entry_requires_reconciliation"


def test_missing_external_id_cannot_erase_known_order_binding(emitted: CoordinationResult) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    bound = replace(order_for(emitted.state), adapter_order_id="observed-id")
    known = coordinator.on_order(emitted.state, bound)
    omitted = replace(
        bound, adapter_order_id=None, updated_at=bound.updated_at + timedelta(seconds=1)
    )
    current = coordinator.on_order(known.state, omitted)
    observed = base_of(current.state).order
    assert observed is not None and observed.adapter_order_id == "observed-id"
    with pytest.raises(ValueError, match="externa"):
        coordinator.on_order(current.state, replace(omitted, adapter_order_id="different-id"))
