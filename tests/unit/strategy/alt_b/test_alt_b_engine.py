"""Señales reales de la evaluación canónica, con historial sintético offline."""

import ast
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

import trading_engine.strategy.alt_b.state_market as market_module
from trading_engine.models.account import Position
from trading_engine.models.decisions import StrategyState
from trading_engine.models.enums import OrderStatus, OrderType, PositionSide, RoundStatus, Side
from trading_engine.models.ids import ActionId, FillId, IntentId, OrderId, RoundId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.values import Price, Quantity, Symbol
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinationResult, CoordinatorState
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.entry import EntryEvaluation, evaluate_entry_checkpoint
from trading_engine.strategy.alt_b.events.engine import AltBEngine
from trading_engine.strategy.alt_b.events.state import (
    ACTION_KIND,
    ENTRY_DELAY,
    SIGNAL_KIND,
    AltBStrategyState,
    PendingEntry,
    entry_identity,
)
from trading_engine.strategy.alt_b.state_market import AltBMarketState
from trading_engine.strategy.contracts import StrategyEngine


def seeded(history: tuple[Candle, ...], *, allowed: bool = True) -> CoordinatorState:
    return CoordinatorState(
        strategy_state=AltBStrategyState(
            market=AltBMarketState(history[:-1]),
            allow_entries=allowed,
            last_event_at=history[-2].close_time,
        )
    )


def state_of(state: CoordinatorState) -> AltBStrategyState:
    assert isinstance(state.strategy_state, AltBStrategyState)
    return state.strategy_state


def quote_at(at: datetime) -> Quote:
    return Quote(Symbol("1000SHIBUSDC"), at, Price(Decimal("100")), Price(Decimal("101")))


def signalled(history: tuple[Candle, ...]) -> tuple[StrategyCoordinator, CoordinationResult]:
    engine: StrategyEngine = AltBEngine()
    coordinator = StrategyCoordinator(engine)
    result = coordinator.on_candle(seeded(history), history[-1])
    assert len(result.signals) == len(result.state.pending_actions) == 1
    return coordinator, result


def test_valid_signal_at_checkpoint_with_exact_delay_and_no_quantity(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    original = seeded(history)
    result = StrategyCoordinator(AltBEngine()).on_candle(original, history[-1])
    assert result.signals == (entry_identity(AltBConfig(), checkpoint)[0],)
    signal = result.signals[0]
    action = result.state.pending_actions[0]
    assert signal.timestamp == action.created_at == checkpoint
    assert signal.kind == SIGNAL_KIND and action.kind == ACTION_KIND
    assert action.not_before == checkpoint + timedelta(seconds=120)
    assert result.order_intents == () and state_of(original).pending_entry is None
    assert state_of(result.state).pending_entry == PendingEntry(signal, action)
    assert not hasattr(action, "quantity") and not hasattr(signal, "quantity")


def test_no_signal_outside_checkpoint(history: tuple[Candle, ...]) -> None:
    coordinator, first = signalled(history)
    # Invalida la reserva y entrega un minuto nuevo, no un checkpoint.
    action = first.state.pending_actions[0]
    cancelled = coordinator.invalidate_action(first.state, action.action_id, action.created_at)
    candle = replace(
        history[-1],
        open_time=action.created_at,
        close_time=action.created_at + timedelta(minutes=1),
    )
    result = coordinator.on_candle(cancelled.state, candle)
    assert result.signals == () and result.order_intents == () and not result.state.pending_actions


def test_warmup_1559_minutes_is_insufficient(history: tuple[Candle, ...]) -> None:
    result = StrategyCoordinator(AltBEngine()).on_candle(seeded(history[1:]), history[-1])
    assert not result.signals and not result.state.pending_actions
    assert state_of(result.state).block_reason == "insufficient_history"


@pytest.mark.parametrize(
    "condition,reason",
    [
        ("filters", "filters_failed"),
        ("veto_a", "veto_a"),
        ("veto_b", "veto_b"),
        ("taker", "missing_current_taker_buy"),
        ("volume", "zero_current_volume"),
        ("med4", "zero_med_vol_4h"),
        ("rv_ref", "zero_rv_ref_24h"),
    ],
)
def test_canonical_filters_vetos_and_invalid_references_prevent_signal(
    history: tuple[Candle, ...], checkpoint: datetime, condition: str, reason: str
) -> None:
    candles = list(history)
    if condition == "filters":
        candles[-1] = replace(candles[-1], low=Price(Decimal("90")), close=Price(Decimal("90")))
    elif condition == "veto_a":
        candles[-1] = replace(candles[-1], close=Price(Decimal("99")))
        candles[-15:] = [
            replace(c, volume=Decimal("8"), taker_buy_base_volume=Decimal("4"))
            for c in candles[-15:]
        ]
    elif condition == "veto_b":
        candles[-31:] = [
            replace(
                c,
                high=Price(Decimal("108")),
                low=Price(Decimal("94")),
                close=Price(Decimal("95") if i % 2 == 0 else Decimal("105")),
            )
            for i, c in enumerate(candles[-31:], start=1)
        ]
        candles[-15:] = [
            replace(c, volume=Decimal("2.5"), taker_buy_base_volume=Decimal("0.625"))
            for c in candles[-15:]
        ]
    elif condition == "taker":
        candles[-1] = replace(candles[-1], taker_buy_base_volume=None)
    elif condition == "volume":
        candles[-15:] = [
            replace(c, volume=Decimal("0"), taker_buy_base_volume=Decimal("0"))
            for c in candles[-15:]
        ]
    elif condition == "med4":
        candles[-255:-15] = [
            replace(c, volume=Decimal("0"), taker_buy_base_volume=None) for c in candles[-255:-15]
        ]
    else:
        candles = [replace(c, close=Price(Decimal("100"))) for c in candles]
    data = tuple(candles)
    # Sin sustituir evaluate_entry_checkpoint por respuestas prefijadas.
    evaluation = evaluate_entry_checkpoint(data, checkpoint, AltBConfig())
    assert not evaluation.entry_allowed and evaluation.reason == reason
    result = StrategyCoordinator(AltBEngine()).on_candle(seeded(data), data[-1])
    assert not result.signals and not result.state.pending_actions and not result.order_intents
    assert state_of(result.state).block_reason == reason


def test_default_control_is_fail_closed_and_requires_explicit_authorization(
    history: tuple[Candle, ...],
) -> None:
    result = StrategyCoordinator(AltBEngine()).on_candle(
        seeded(history, allowed=False), history[-1]
    )
    assert not result.signals and state_of(result.state).block_reason == "entries_paused"
    result = StrategyCoordinator(AltBEngine()).on_candle(CoordinatorState(), history[0])
    assert not state_of(result.state).allow_entries


@pytest.mark.parametrize(
    "status", [RoundStatus.PENDING_ENTRY, RoundStatus.OPEN, RoundStatus.CLOSING]
)
def test_active_round_prevents_signal(history: tuple[Candle, ...], status: RoundStatus) -> None:
    at = history[-1].close_time
    active = Round(
        RoundId(UUID(int=1)),
        history[-1].symbol,
        status,
        at,
        opened_at=None if status is RoundStatus.PENDING_ENTRY else at,
    )
    result = StrategyCoordinator(AltBEngine()).on_candle(
        seeded(history), history[-1], round_state=active
    )
    assert not result.signals and state_of(result.state).block_reason == "round_active"


@pytest.mark.parametrize("side", [PositionSide.LONG, PositionSide.SHORT])
def test_any_position_prevents_signal(history: tuple[Candle, ...], side: PositionSide) -> None:
    position = Position(
        history[-1].symbol,
        side,
        Quantity(Decimal("1")),
        Price(Decimal("100")),
        history[-1].close_time,
    )
    result = StrategyCoordinator(AltBEngine()).on_candle(
        seeded(history), history[-1], position=position
    )
    assert not result.signals and state_of(result.state).block_reason == "position_present"


def test_pending_entry_prevents_second_signal_at_next_valid_checkpoint(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    coordinator, first = signalled(history)
    state = first.state
    for offset in range(15):
        candle = replace(
            history[offset],
            open_time=checkpoint + timedelta(minutes=offset),
            close_time=checkpoint + timedelta(minutes=offset + 1),
        )
        result = coordinator.on_candle(state, candle)
        assert not result.signals and not result.order_intents
        state = result.state
    assert state.pending_actions == first.state.pending_actions
    assert state_of(state).pending_entry == state_of(first.state).pending_entry


def test_duplicate_and_checkpoint_retry_never_reevaluate_or_emit(
    history: tuple[Candle, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, first = signalled(history)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("No repetir evaluación del checkpoint")

    monkeypatch.setattr(market_module, "evaluate_entry_checkpoint", forbidden)
    for _ in range(3):
        result = coordinator.on_candle(first.state, history[-1])
        assert not result.signals and not result.order_intents and result.state == first.state


def test_gap_cancels_pending_entry_and_restarts_full_warmup(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    coordinator, first = signalled(history)
    jumped = replace(
        history[-1],
        open_time=checkpoint + timedelta(minutes=1),
        close_time=checkpoint + timedelta(minutes=2),
    )
    result = coordinator.on_candle(first.state, jumped)
    assert state_of(result.state).market.candles_1m == (jumped,)
    assert not result.signals and not result.state.pending_actions
    assert state_of(result.state).pending_entry is None
    assert state_of(result.state).block_reason == "gap_reset"


def test_recovery_after_missing_minute_requires_1560_new_continuous_minutes(
    history: tuple[Candle, ...],
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    start = history[0].open_time - timedelta(minutes=2)
    enabled = coordinator.on_entry_control(CoordinatorState(), True, start).state
    before = replace(history[0], open_time=start, close_time=start + timedelta(minutes=1))
    state = coordinator.on_candle(enabled, before).state
    # Falta el minuto anterior al primer elemento de history.
    for i, candle in enumerate(history):
        result = coordinator.on_candle(state, candle)
        assert bool(result.signals) is (i == 1559)
        state = result.state
    assert state_of(state).market.candles_1m == history


@pytest.mark.parametrize("seconds", [0, 119])
def test_before_120_seconds_is_rejected_by_coordinator_and_engine(
    history: tuple[Candle, ...], seconds: int
) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    quote = quote_at(action.created_at + timedelta(seconds=seconds))
    assert not coordinator.advance_time(first.state, quote.timestamp).due_actions
    with pytest.raises(ValueError, match="not_before"):
        coordinator.on_scheduled_action(first.state, action, quote)
    with pytest.raises(ValueError, match="not_before"):
        AltBEngine().on_scheduled_action(
            action, quote, None, None, strategy_state=first.state.strategy_state
        )
    assert state_of(first.state).pending_entry == PendingEntry(first.signals[0], action)


def test_exact_120_seconds_due_consumes_action_with_one_theoretical_intent(
    history: tuple[Candle, ...],
) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    due = coordinator.advance_time(first.state, action.not_before)
    assert due.due_actions == (action,) and not due.state.pending_actions
    assert due.state.strategy_state is first.state.strategy_state
    result = coordinator.on_scheduled_action(due.state, action, quote_at(action.not_before))
    assert not result.signals and len(result.order_intents) == 1
    assert not result.state.pending_actions and state_of(result.state).pending_entry is None
    base = state_of(result.state).base
    assert base is not None and base.intent == result.order_intents[0]
    assert base.action == action and base.intent.created_at == action.not_before
    assert base.order is None and base.fills == () and base.p0 is None
    retry = coordinator.on_scheduled_action(result.state, action, quote_at(action.not_before))
    assert retry.state == result.state and not retry.order_intents


def test_pause_during_wait_cancels_queue_and_resume_never_runs_old_signal(
    history: tuple[Candle, ...],
) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    paused = coordinator.on_entry_control(
        first.state, False, action.created_at + timedelta(seconds=30)
    )
    assert not paused.state.pending_actions and state_of(paused.state).pending_entry is None
    assert not state_of(paused.state).allow_entries
    resumed = coordinator.on_entry_control(
        paused.state, True, action.created_at + timedelta(seconds=60)
    )
    assert not resumed.signals and not resumed.order_intents
    # Retry de la vela de señal anterior a RESUME: nunca reevalúa.
    duplicate = coordinator.on_candle(resumed.state, history[-1])
    assert not duplicate.signals and not duplicate.state.pending_actions
    stale = coordinator.on_scheduled_action(duplicate.state, action, quote_at(action.not_before))
    assert not stale.order_intents and state_of(stale.state).pending_entry is None


def test_explicit_action_invalidation_and_retry_are_idempotent(history: tuple[Candle, ...]) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    at = action.created_at + timedelta(seconds=1)
    result = coordinator.invalidate_action(first.state, action.action_id, at)
    assert not result.state.pending_actions and state_of(result.state).pending_entry is None
    assert coordinator.invalidate_action(result.state, action.action_id, at) == result
    result = coordinator.on_scheduled_action(result.state, action, quote_at(action.not_before))
    assert not result.order_intents and not result.signals


@pytest.mark.parametrize("incompatible", ["position", "round", "identity"])
def test_pending_entry_invalidates_on_incompatible_context_or_action(
    history: tuple[Candle, ...], incompatible: str
) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    position = None
    round_state = None
    if incompatible == "position":
        position = Position(
            action.symbol,
            PositionSide.LONG,
            Quantity(Decimal("1")),
            Price(Decimal("100")),
            action.not_before,
        )
    elif incompatible == "round":
        round_state = Round(
            RoundId(UUID(int=99)), action.symbol, RoundStatus.PENDING_ENTRY, action.not_before
        )
    else:
        action = replace(action, action_id=ActionId(UUID(int=99)))
    result = coordinator.on_scheduled_action(
        first.state, action, quote_at(action.not_before), position=position, round_state=round_state
    )
    assert not result.signals and not result.order_intents and not result.state.pending_actions
    assert state_of(result.state).pending_entry is None


def test_quote_safety_observation_cancels_without_waiting_for_due(
    history: tuple[Candle, ...],
) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    at = action.created_at + timedelta(seconds=1)
    position = Position(
        action.symbol, PositionSide.LONG, Quantity(Decimal("1")), Price(Decimal("100")), at
    )
    result = coordinator.on_quote(first.state, quote_at(at), position=position)
    assert not result.state.pending_actions and state_of(result.state).pending_entry is None
    assert not result.order_intents


def test_snapshot_reconstruction_and_same_engine_reuse_are_deterministic(
    history: tuple[Candle, ...],
) -> None:
    engine = AltBEngine()
    state = seeded(history)
    one = StrategyCoordinator(engine).on_candle(state, history[-1])
    two = StrategyCoordinator(engine).on_candle(replace(state), history[-1])
    assert one == two
    reconstructed = replace(one.state, strategy_state=replace(state_of(one.state)))
    action = one.state.pending_actions[0]
    for snapshot in (one.state, reconstructed):
        due = StrategyCoordinator(engine).advance_time(snapshot, action.not_before)
        result = StrategyCoordinator(engine).on_scheduled_action(
            due.state, action, quote_at(action.not_before)
        )
        assert len(result.order_intents) == 1
        assert state_of(result.state).base is not None
        assert state_of(result.state).block_reason == "base_order_pending"
    assert tuple(f.name for f in fields(engine)) == ("config",)


def test_canonical_signal_round_and_action_uuid_v5_golden_vector(
    history: tuple[Candle, ...],
) -> None:
    _, result = signalled(history)
    signal = result.signals[0]
    action = result.state.pending_actions[0]
    assert str(signal.signal_id) == "562f6059-cb33-5ccc-b208-68a643d24e07"
    assert str(action.round_id) == "ebd0da91-6e93-58c4-91ca-805a6568b56e"
    assert str(action.action_id) == "ec7ccf71-85e9-5cc3-b976-fa85b7dd9648"
    assert all(
        identifier.version == 5
        for identifier in (signal.signal_id, action.round_id, action.action_id)
    )


def test_full_history_reconstruction_matches_incremental_events(
    history: tuple[Candle, ...],
) -> None:
    def run(reconstruct_market: bool) -> tuple[tuple[CoordinationResult, ...], CoordinatorState]:
        coordinator = StrategyCoordinator(AltBEngine())
        state = coordinator.on_entry_control(CoordinatorState(), True, history[0].open_time).state
        checkpoints: list[CoordinationResult] = []
        for i, candle in enumerate(history):
            if reconstruct_market:
                state = replace(
                    state,
                    strategy_state=replace(
                        state_of(state), market=AltBMarketState(history[max(0, i - 1560) : i])
                    ),
                )
            result = coordinator.on_candle(state, candle)
            state = result.state
            if candle.close_time.minute % 15 == 0:
                checkpoints.append(result)
        return tuple(checkpoints), state

    assert run(True) == run(False)


def test_future_operational_context_cannot_authorize_past_checkpoint(
    history: tuple[Candle, ...],
) -> None:
    coordinator = StrategyCoordinator(AltBEngine())
    initial = seeded(history, allowed=False)
    enabled = coordinator.on_entry_control(initial, True, history[-1].close_time + ENTRY_DELAY)
    with pytest.raises(ValueError, match="cursor causal"):
        coordinator.on_candle(enabled.state, history[-1])
    assert not enabled.signals and not enabled.state.pending_actions


def test_future_round_snapshot_fails_closed(history: tuple[Candle, ...]) -> None:
    at = history[-1].close_time + ENTRY_DELAY
    round_state = Round(RoundId(UUID(int=90)), history[-1].symbol, RoundStatus.FAILED, at)
    result = StrategyCoordinator(AltBEngine()).on_candle(
        seeded(history), history[-1], round_state=round_state
    )
    assert (
        not result.signals and state_of(result.state).block_reason == "round_context_unverifiable"
    )


def test_conflicting_duplicate_and_older_candle_fail_without_mutation(
    history: tuple[Candle, ...],
) -> None:
    coordinator, first = signalled(history)
    for candle in (replace(history[-1], volume=Decimal("2")), history[-2]):
        with pytest.raises(ValueError):
            coordinator.on_candle(first.state, candle)
    assert state_of(first.state).market.candles_1m == history


def test_state_and_engine_are_frozen_and_reject_unverifiable_state(
    history: tuple[Candle, ...],
) -> None:
    _, first = signalled(history)
    for snapshot in (AltBEngine(), state_of(first.state), state_of(first.state).pending_entry):
        assert snapshot is not None
        for field in fields(snapshot):
            with pytest.raises(FrozenInstanceError):
                setattr(snapshot, field.name, None)
    with pytest.raises(TypeError):
        AltBEngine().on_candle(history[-1], None, None, strategy_state=StrategyState())
    with pytest.raises(TypeError):
        replace(state_of(first.state), allow_entries=cast(bool, 1))
    pending = state_of(first.state).pending_entry
    assert pending is not None
    with pytest.raises(ValueError, match="Identidad"):
        replace(
            pending,
            action=replace(
                pending.action, not_before=pending.action.not_before + timedelta(seconds=1)
            ),
        )


def test_engine_dependencies_have_no_io_clock_or_random_uuid() -> None:
    root = Path(__file__).resolve().parents[4] / "src/trading_engine/strategy/alt_b/events"
    for filename in ("engine.py", "state.py"):
        for node in ast.walk(ast.parse((root / filename).read_text())):
            if isinstance(node, ast.Import):
                pytest.fail("El motor solo importa snapshots y utilidades puras")
            if isinstance(node, ast.ImportFrom):
                assert node.module is not None
                assert node.module in {
                    "dataclasses",
                    "datetime",
                    "typing",
                } or node.module.startswith(
                    ("trading_engine.models.", "trading_engine.strategy.alt_b.")
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"now", "utcnow", "sleep", "uuid4", "connect"}


def test_reconstruction_rejects_queue_without_matching_logical_entry(
    history: tuple[Candle, ...],
) -> None:
    _, first = signalled(history)
    action = first.state.pending_actions[0]
    with pytest.raises(ValueError, match="scheduling"):
        replace(first.state, strategy_state=replace(state_of(first.state), pending_entry=None))
    with pytest.raises(ValueError, match="scheduling"):
        replace(first.state, pending_actions=(replace(action, action_id=ActionId(UUID(int=99))),))
    # Reserva válida aun si la acción ya fue extraída de la cola por advance_time.
    assert state_of(replace(first.state, pending_actions=())).pending_entry is not None


def test_blocked_checkpoint_duplicate_preserves_reason_and_state(
    history: tuple[Candle, ...],
) -> None:
    short = history[1:]
    coordinator = StrategyCoordinator(AltBEngine())
    first = coordinator.on_candle(seeded(short), short[-1])
    retry = coordinator.on_candle(first.state, short[-1])
    assert not retry.signals and retry.state == first.state
    assert state_of(retry.state).block_reason == "insufficient_history"


@pytest.mark.parametrize("event", ["order", "fill"])
def test_execution_without_base_reservation_is_fail_closed(
    history: tuple[Candle, ...], event: str
) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    at = action.created_at + timedelta(seconds=1)
    intent = OrderIntent(
        IntentId(UUID(int=1)),
        action.round_id,
        action.symbol,
        Side.BUY,
        OrderType.MARKET,
        Quantity(Decimal("1")),
        at,
    )
    order = Order(OrderId(UUID(int=2)), intent, OrderStatus.OPEN, Decimal("0"), at, at)
    if event == "order":
        result = coordinator.on_order(first.state, order)
    else:
        fill = Fill(
            FillId(UUID(int=3)), order.order_id, at, Price(Decimal("100")), Quantity(Decimal("1"))
        )
        result = coordinator.on_fill(first.state, fill, order)
    assert not result.order_intents and not result.signals and not result.state.pending_actions
    assert not state_of(result.state).allow_entries and state_of(result.state).pending_entry is None
    assert state_of(result.state).block_reason == "execution_without_base_reservation"


@pytest.mark.parametrize("event", ["control", "cancel", "quote"])
def test_old_control_cancel_and_quote_never_use_future_state(
    history: tuple[Candle, ...], event: str
) -> None:
    coordinator, first = signalled(history)
    action = first.state.pending_actions[0]
    at = action.created_at - timedelta(seconds=1)
    with pytest.raises(ValueError, match="cursor causal"):
        if event == "control":
            coordinator.on_entry_control(first.state, False, at)
        elif event == "cancel":
            coordinator.invalidate_action(first.state, action.action_id, at)
        else:
            coordinator.on_quote(first.state, quote_at(at))
    assert first.state.pending_actions == (action,)


@pytest.mark.parametrize("incorrect", ["duration", "symbol"])
def test_only_normalized_configured_minutes_are_accepted(
    history: tuple[Candle, ...], incorrect: str
) -> None:
    candle = history[-1]
    if incorrect == "duration":
        candle = replace(candle, close_time=candle.close_time + timedelta(seconds=1))
    else:
        candle = replace(candle, symbol=Symbol("1000SHIBUSDT"))
    with pytest.raises(ValueError):
        StrategyCoordinator(AltBEngine()).on_candle(seeded(history), candle)


def test_every_evaluation_uses_only_causal_prefix_and_never_quote_prices(
    history: tuple[Candle, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[datetime] = []
    original = evaluate_entry_checkpoint

    def evaluate(candles: tuple[Candle, ...], at: datetime, config: AltBConfig) -> EntryEvaluation:
        assert all(c.close_time <= at for c in candles)
        assert candles[-1].close_time == at and at.minute % 15 == 0
        seen.append(at)
        return original(candles, at, config)

    monkeypatch.setattr(market_module, "evaluate_entry_checkpoint", evaluate)
    coordinator = StrategyCoordinator(AltBEngine())
    result = coordinator.on_candle(seeded(history), history[-1])
    action = result.state.pending_actions[0]
    quote = replace(
        quote_at(action.not_before), bid=Price(Decimal("10000")), ask=Price(Decimal("10001"))
    )
    due = coordinator.advance_time(result.state, action.not_before)
    evaluated = coordinator.on_scheduled_action(due.state, action, quote)
    assert seen == [action.created_at] and len(evaluated.order_intents) == 1
    base = state_of(evaluated.state).base
    assert base is not None and base.reference_quote == quote
    assert base.fills == () and base.p0 is None
