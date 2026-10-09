import ast
import time
import uuid
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from itertools import permutations
from pathlib import Path
from typing import Self, cast
from uuid import UUID

import pytest

import trading_engine.orchestration.coordinator as coordinator_module
from trading_engine.models.account import Position
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyResult, StrategyState
from trading_engine.models.ids import ActionId, OrderId, SignalId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.signals import Signal
from trading_engine.models.values import Quantity, Symbol
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import (
    CoordinationResult,
    CoordinatorState,
    DueActionsResult,
)

HANDLERS = ("on_candle", "on_quote", "on_order", "on_fill", "on_scheduled_action")


class CannedStrategy:
    def __init__(self, responses: tuple[StrategyResult, ...]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def _respond(self, handler: str, *arguments: object) -> StrategyResult:
        index = len(self.calls)
        self.calls.append((handler, arguments))
        return self.responses[index]

    def on_candle(
        self,
        candle: Candle,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        return self._respond("on_candle", candle, position, round_state)

    def on_quote(
        self,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        return self._respond("on_quote", quote, position, round_state)

    def on_order(
        self,
        order: Order,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        return self._respond("on_order", order, position, round_state)

    def on_fill(
        self,
        fill: Fill,
        order: Order,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        return self._respond("on_fill", fill, order, position, round_state)

    def on_scheduled_action(
        self,
        action: ScheduledAction,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        return self._respond("on_scheduled_action", action, quote, position, round_state)

    def on_entry_control(
        self,
        allow_entries: bool,
        at: datetime,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        return self._respond("control", allow_entries, at, position, round_state)

    def on_action_invalidated(
        self,
        action_id: ActionId,
        at: datetime,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        return self._respond("invalidate", action_id, at, position, round_state)


def scheduled(action: ScheduledAction, identifier: int, seconds: int) -> ScheduledAction:
    return replace(
        action,
        action_id=ActionId(UUID(int=identifier)),
        not_before=action.created_at + timedelta(seconds=seconds),
    )


def event_arguments(
    handler: str,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
    action: ScheduledAction,
) -> dict[str, object]:
    arguments: dict[str, dict[str, object]] = {
        "on_candle": {"candle": candle},
        "on_quote": {"quote": quote},
        "on_order": {"order": order},
        "on_fill": {"fill": fill, "order": order},
        "on_scheduled_action": {"action": action, "quote": quote},
    }
    return arguments[handler]


def causal_time(handler: str, candle: Candle, quote: Quote, order: Order, fill: Fill) -> datetime:
    return {
        "on_candle": candle.close_time,
        "on_quote": quote.timestamp,
        "on_order": order.updated_at,
        "on_fill": fill.timestamp,
        "on_scheduled_action": quote.timestamp,
    }[handler]


def test_empty_state_and_time_advance() -> None:
    state = CoordinatorState()
    strategy = CannedStrategy(())
    result = StrategyCoordinator(strategy).advance_time(state, datetime(2026, 1, 1, tzinfo=UTC))
    assert result == DueActionsResult((), CoordinatorState())
    assert strategy.calls == []


def test_state_canonicalizes_eligibility_then_uuid(action: ScheduledAction) -> None:
    early = scheduled(action, 20, 0)
    low_id = scheduled(action, 2, 17)
    high_id = scheduled(action, 10, 17)
    expected = (early, low_id, high_id)
    for insertion_order in permutations(expected):
        assert CoordinatorState(insertion_order).pending_actions == expected
    assert CoordinatorState((action,)).pending_actions == (action,)


@pytest.mark.parametrize("pending", [[], None, ("invalid",)])
def test_state_rejects_wrong_types(pending: object) -> None:
    with pytest.raises(TypeError):
        CoordinatorState(cast(tuple[ScheduledAction, ...], pending))


def test_state_rejects_duplicate_ids_even_if_other_fields_differ(action: ScheduledAction) -> None:
    with pytest.raises(ValueError, match="action_id duplicado"):
        CoordinatorState((action, replace(action, kind="other")))


def test_state_and_results_are_immutable(action: ScheduledAction) -> None:
    state = CoordinatorState((action,))
    coordination = CoordinationResult((), (), state)
    due = DueActionsResult((action,), CoordinatorState())
    for value, field in (
        (state, "pending_actions"),
        (coordination, "signals"),
        (coordination, "order_intents"),
        (coordination, "state"),
        (due, "due_actions"),
        (due, "state"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)


@pytest.mark.parametrize(
    "changes",
    [
        {"signals": []},
        {"signals": ("invalid",)},
        {"state": None},
        {"order_intents": []},
        {"order_intents": ("invalid",)},
    ],
)
def test_coordination_result_rejects_invalid_types(changes: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        replace(CoordinationResult((), (), CoordinatorState()), **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes", [{"due_actions": []}, {"due_actions": ("invalid",)}, {"state": None}]
)
def test_due_result_rejects_invalid_types(changes: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        replace(DueActionsResult((), CoordinatorState()), **changes)  # type: ignore[arg-type]


def test_due_result_canonicalizes_and_rejects_duplicates(action: ScheduledAction) -> None:
    earlier = scheduled(action, 2, 0)
    later = scheduled(action, 3, 7)
    assert DueActionsResult((later, earlier), CoordinatorState()).due_actions == (earlier, later)
    with pytest.raises(ValueError):
        DueActionsResult((action, action), CoordinatorState())


@pytest.mark.parametrize("handler", HANDLERS)
def test_handler_calls_once_preserves_outputs_and_only_schedules_actions(
    handler: str,
    intent: OrderIntent,
    action: ScheduledAction,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
    position: Position,
    round_state: Round,
) -> None:
    event_time = causal_time(handler, candle, quote, order, fill)
    output_action = replace(action, created_at=event_time, not_before=event_time)
    intent = replace(intent, created_at=event_time)
    existing = scheduled(output_action, 20, 5)
    immediate = scheduled(output_action, 2, 0)
    deferred = scheduled(output_action, 3, 31)
    signals = (
        Signal(SignalId(UUID(int=8)), candle.symbol, candle.close_time, "second"),
        Signal(SignalId(UUID(int=7)), candle.symbol, candle.close_time, "first"),
    )
    intents = (intent, replace(intent, reduce_only=True))
    strategy = CannedStrategy((StrategyResult(signals, (deferred, immediate), intents),))
    state = CoordinatorState((existing,))
    arguments = event_arguments(handler, candle, quote, order, fill, action)
    result = getattr(StrategyCoordinator(strategy), handler)(
        state, **arguments, position=position, round_state=round_state
    )
    assert strategy.calls == [(handler, (*arguments.values(), position, round_state))]
    assert result.signals is signals
    assert result.order_intents is intents
    assert result.state.pending_actions == (immediate, existing, deferred)
    assert state.pending_actions == (existing,)
    assert result.state is not state


def test_signal_alone_does_not_create_intent_action_or_round(candle: Candle) -> None:
    signal = Signal(SignalId(UUID(int=7)), candle.symbol, candle.close_time, "observation")
    strategy = CannedStrategy((StrategyResult(signals=(signal,)),))
    result = StrategyCoordinator(strategy).on_candle(CoordinatorState(), candle)
    assert result.signals == (signal,)
    assert result.order_intents == ()
    assert result.state == CoordinatorState()
    assert strategy.calls == [("on_candle", (candle, None, None))]


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize("duplicate_existing", [False, True])
def test_handlers_reject_duplicate_action_ids(
    handler: str,
    action: ScheduledAction,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
    duplicate_existing: bool,
) -> None:
    state = CoordinatorState((action,)) if duplicate_existing else CoordinatorState()
    event_time = causal_time(handler, candle, quote, order, fill)
    produced_action = replace(action, created_at=event_time, not_before=event_time)
    produced = (produced_action,) if duplicate_existing else (produced_action, produced_action)
    strategy = CannedStrategy((StrategyResult(scheduled_actions=produced),))
    with pytest.raises(ValueError, match="action_id duplicado"):
        getattr(StrategyCoordinator(strategy), handler)(
            state, **event_arguments(handler, candle, quote, order, fill, action)
        )
    assert len(strategy.calls) == 1
    assert state.pending_actions == ((action,) if duplicate_existing else ())


@pytest.mark.parametrize("handler", HANDLERS)
def test_handlers_empty_result_preserves_pending(
    handler: str,
    action: ScheduledAction,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
) -> None:
    state = CoordinatorState((action,))
    strategy = CannedStrategy((StrategyResult(),))
    result = getattr(StrategyCoordinator(strategy), handler)(
        state, **event_arguments(handler, candle, quote, order, fill, action)
    )
    assert result.signals == () and result.order_intents == () and result.state == state
    assert len(strategy.calls) == 1


@pytest.mark.parametrize("handler", HANDLERS)
def test_bad_strategy_result_and_errors_are_not_hidden(
    handler: str,
    action: ScheduledAction,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
) -> None:
    arguments = event_arguments(handler, candle, quote, order, fill, action)
    invalid = CannedStrategy((cast(StrategyResult, ()),))
    with pytest.raises(TypeError, match="strategy_result"):
        getattr(StrategyCoordinator(invalid), handler)(CoordinatorState(), **arguments)
    assert len(invalid.calls) == 1
    failing = CannedStrategy(())
    with pytest.raises(IndexError):
        getattr(StrategyCoordinator(failing), handler)(CoordinatorState(), **arguments)
    assert len(failing.calls) == 1


@pytest.mark.parametrize("seconds, eligible", [(0, False), (16, False), (17, True), (18, True)])
def test_explicit_action_boundary(action: ScheduledAction, seconds: int, eligible: bool) -> None:
    deferred = scheduled(action, 2, 17)
    state = CoordinatorState((deferred,))
    strategy = CannedStrategy(())
    coordinator = StrategyCoordinator(strategy)
    result = coordinator.advance_time(state, action.created_at + timedelta(seconds=seconds))
    assert result.due_actions == ((deferred,) if eligible else ())
    assert result.state.pending_actions == (() if eligible else (deferred,))
    assert state.pending_actions == (deferred,)
    assert strategy.calls == []
    if eligible:
        assert (
            coordinator.advance_time(
                result.state, action.created_at + timedelta(seconds=19)
            ).due_actions
            == ()
        )


def test_advance_partitions_multiple_actions_in_canonical_order(action: ScheduledAction) -> None:
    early = scheduled(action, 8, 0)
    tied_low = scheduled(action, 2, 7)
    tied_high = scheduled(action, 9, 7)
    later = scheduled(action, 1, 8)
    state = CoordinatorState((later, tied_high, early, tied_low))
    coordinator = StrategyCoordinator(CannedStrategy(()))
    result = coordinator.advance_time(state, action.created_at + timedelta(seconds=7))
    assert result.due_actions == (early, tied_low, tied_high)
    assert result.due_actions[-1] is tied_high
    assert result.state.pending_actions == (later,)
    final = coordinator.advance_time(result.state, action.created_at + timedelta(seconds=8))
    assert final.due_actions == (later,) and final.state == CoordinatorState()


@pytest.mark.parametrize(
    "now, error",
    [
        (datetime(2026, 1, 1), ValueError),
        (datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))), ValueError),
        ("now", TypeError),
        (None, TypeError),
    ],
)
def test_advance_requires_explicit_utc(now: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        StrategyCoordinator(CannedStrategy(())).advance_time(
            CoordinatorState(), cast(datetime, now)
        )


def test_repeating_the_same_event_sequence_is_deterministic(
    action: ScheduledAction,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
) -> None:
    action = replace(action, created_at=candle.close_time, not_before=candle.close_time)
    quote = replace(quote, timestamp=action.created_at + timedelta(seconds=7))
    resolved_intent = replace(order.intent, created_at=quote.timestamp)
    order = replace(
        order, intent=resolved_intent, created_at=quote.timestamp, updated_at=quote.timestamp
    )
    fill = replace(fill, timestamp=quote.timestamp)
    early = scheduled(action, 2, 7)
    late = scheduled(action, 3, 19)
    signals = (
        Signal(SignalId(UUID(int=6)), candle.symbol, candle.close_time, "b"),
        Signal(SignalId(UUID(int=5)), candle.symbol, candle.close_time, "a"),
    )
    responses = (
        StrategyResult(signals, (late, early)),
        StrategyResult(),
        StrategyResult(order_intents=(order.intent,)),
        StrategyResult(),
        StrategyResult(),
    )

    def run() -> tuple[object, ...]:
        coordinator = StrategyCoordinator(CannedStrategy(responses))
        first = coordinator.on_candle(CoordinatorState(), candle)
        observed = coordinator.on_quote(first.state, quote)
        due = coordinator.advance_time(observed.state, action.created_at + timedelta(seconds=7))
        evaluated = coordinator.on_scheduled_action(due.state, due.due_actions[0], quote)
        accepted = coordinator.on_order(evaluated.state, order)
        filled = coordinator.on_fill(accepted.state, fill, order)
        final = coordinator.advance_time(filled.state, action.created_at + timedelta(seconds=19))
        return first, observed, due, evaluated, accepted, filled, final

    assert run() == run()


def test_coordinator_uses_no_system_clock_or_generated_ids(
    monkeypatch: pytest.MonkeyPatch,
    action: ScheduledAction,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("No se permiten efectos externos")

    class NoClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            raise AssertionError("No se permite reloj interno")

    monkeypatch.setattr(coordinator_module, "datetime", NoClock)
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    monkeypatch.setattr(uuid, "uuid4", forbidden)
    action = replace(action, created_at=candle.close_time, not_before=candle.close_time)
    quote = replace(quote, timestamp=action.not_before)
    strategy = CannedStrategy(
        (StrategyResult(scheduled_actions=(action,)),) + (StrategyResult(),) * 4
    )
    coordinator = StrategyCoordinator(strategy)
    result = coordinator.on_candle(CoordinatorState(), candle)
    due = coordinator.advance_time(result.state, action.not_before)
    coordinator.on_quote(due.state, quote)
    coordinator.on_order(due.state, order)
    coordinator.on_fill(due.state, fill, order)
    coordinator.on_scheduled_action(due.state, action, quote)
    assert due.due_actions == (action,)
    assert len(strategy.calls) == 5


@pytest.mark.parametrize("handler", HANDLERS)
def test_handlers_invalid_arguments_fail_before_strategy(
    handler: str,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
    action: ScheduledAction,
) -> None:
    valid: dict[str, object] = {
        "state": CoordinatorState(),
        **event_arguments(handler, candle, quote, order, fill, action),
        "position": None,
        "round_state": None,
    }
    for field in valid:
        strategy = CannedStrategy((StrategyResult(),))
        with pytest.raises(TypeError):
            getattr(StrategyCoordinator(strategy), handler)(**{**valid, field: "invalid"})
        assert strategy.calls == []


def test_handler_rejects_fill_for_another_order(fill: Fill, order: Order) -> None:
    strategy = CannedStrategy((StrategyResult(),))
    with pytest.raises(ValueError, match="order_id"):
        StrategyCoordinator(strategy).on_fill(
            CoordinatorState(), replace(fill, order_id=OrderId(UUID(int=99))), order
        )
    assert strategy.calls == []


def test_handler_rejects_quote_for_another_symbol(action: ScheduledAction, quote: Quote) -> None:
    strategy = CannedStrategy((StrategyResult(),))
    with pytest.raises(ValueError, match="symbol"):
        StrategyCoordinator(strategy).on_scheduled_action(
            CoordinatorState(), action, replace(quote, symbol=Symbol("OTHER"))
        )
    assert strategy.calls == []


def test_advance_rejects_wrong_state_without_calling_strategy() -> None:
    strategy = CannedStrategy(())
    with pytest.raises(TypeError):
        StrategyCoordinator(strategy).advance_time(
            cast(CoordinatorState, ()), datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert strategy.calls == []


def test_orchestration_imports_only_its_allowed_dependencies() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "trading_engine" / "orchestration"
    modules = list(root.glob("*.py"))
    assert modules
    for module in modules:
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Import):
                pytest.fail(f"Import inesperado en {module}")
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                assert node.module is not None
                assert node.module in {
                    "datetime",
                    "dataclasses",
                    "trading_engine.strategy.contracts",
                } or node.module.startswith(
                    ("trading_engine.models.", "trading_engine.orchestration.")
                )


def test_core_has_no_clock_randomness_or_generated_ids() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "trading_engine"
    modules = list((root / "models").glob("*.py")) + list((root / "orchestration").glob("*.py"))
    modules += list((root / "strategy").glob("*.py"))
    for module in modules:
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Import):
                assert not any(alias.name in {"random", "time"} for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in {"random", "time"}
            elif isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else (node.func.id if isinstance(node.func, ast.Name) else "")
                )
                assert name not in {"now", "utcnow", "time", "sleep", "uuid4"}, module


def test_due_action_uses_current_quote_and_fill_remains_independent(
    candle: Candle,
    action: ScheduledAction,
    quote: Quote,
    intent: OrderIntent,
    order: Order,
    fill: Fill,
) -> None:
    deferred = replace(
        action, created_at=candle.close_time, not_before=candle.close_time + timedelta(seconds=37)
    )
    signal = Signal(SignalId(UUID(int=7)), candle.symbol, candle.close_time, "observation")

    class QuoteSizingFake(CannedStrategy):
        def on_scheduled_action(
            self,
            action: ScheduledAction,
            quote: Quote,
            position: Position | None,
            round_state: Round | None,
            *,
            strategy_state: StrategyState | None = None,
        ) -> StrategyResult:
            self.calls.append(("on_scheduled_action", (action, quote, position, round_state)))
            # Sizing ilustrativo del fake; ninguna regla de Alternativa B.
            resolved = replace(
                intent,
                created_at=quote.timestamp,
                quantity=Quantity(Decimal("11") / quote.ask.value),
            )
            return StrategyResult(order_intents=(resolved,))

    strategy = QuoteSizingFake((StrategyResult(signals=(signal,), scheduled_actions=(deferred,)),))
    coordinator = StrategyCoordinator(strategy)
    first = coordinator.on_candle(CoordinatorState(), candle)
    assert first.signals == (signal,)
    assert first.order_intents == ()
    assert not hasattr(first.state.pending_actions[0], "quantity")
    due = coordinator.advance_time(first.state, deferred.not_before)
    assert len(strategy.calls) == 1  # Extraer due no ejecuta la acción.
    current_quote = replace(quote, timestamp=deferred.not_before)
    resolved = coordinator.on_scheduled_action(due.state, due.due_actions[0], current_quote)
    assert resolved.state.pending_actions == ()
    assert resolved.order_intents[0].created_at == current_quote.timestamp
    assert resolved.order_intents[0].quantity.value == Decimal("11") / current_quote.ask.value
    executed_order = replace(
        order,
        intent=resolved.order_intents[0],
        created_at=current_quote.timestamp,
        updated_at=current_quote.timestamp,
    )
    actual_fill = replace(fill, timestamp=current_quote.timestamp)
    # El adaptador/modelo de ejecución puede llenar a un precio diferente del Quote.
    assert actual_fill.price != current_quote.ask
    assert executed_order.filled_quantity == 0
    strategy.responses += (StrategyResult(), StrategyResult(), StrategyResult())
    quoted = coordinator.on_quote(resolved.state, current_quote)
    assert executed_order.filled_quantity == 0
    assert strategy.calls[-1] == ("on_quote", (current_quote, None, None))
    filled = coordinator.on_fill(quoted.state, actual_fill, executed_order)
    assert strategy.calls[-1] == ("on_fill", (actual_fill, executed_order, None, None))
    assert filled.order_intents == ()
    assert filled.state == CoordinatorState()


def test_scheduled_action_rejects_early_quote_before_calling_strategy(
    action: ScheduledAction,
    quote: Quote,
) -> None:
    deferred = scheduled(action, 2, 37)
    early_quote = replace(quote, timestamp=deferred.not_before - timedelta(microseconds=1))
    state = CoordinatorState((deferred,))
    strategy = CannedStrategy((StrategyResult(),))
    with pytest.raises(ValueError, match="not_before"):
        StrategyCoordinator(strategy).on_scheduled_action(state, deferred, early_quote)
    assert strategy.calls == []
    assert state.pending_actions == (deferred,)
    assert early_quote.timestamp == deferred.not_before - timedelta(microseconds=1)


@pytest.mark.parametrize("microseconds", [0, 1])
def test_scheduled_action_accepts_quote_at_or_after_eligibility(
    action: ScheduledAction,
    quote: Quote,
    microseconds: int,
) -> None:
    deferred = scheduled(action, 2, 37)
    current_quote = replace(
        quote, timestamp=deferred.not_before + timedelta(microseconds=microseconds)
    )
    strategy = CannedStrategy((StrategyResult(),))
    result = StrategyCoordinator(strategy).on_scheduled_action(
        CoordinatorState(), deferred, current_quote
    )
    assert strategy.calls == [("on_scheduled_action", (deferred, current_quote, None, None))]
    assert result == CoordinationResult((), (), CoordinatorState())


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize("output_kind", ["action", "intent"])
@pytest.mark.parametrize("microseconds", [-1, 0, 1])
def test_handlers_enforce_output_creation_at_or_after_causal_event(
    handler: str,
    output_kind: str,
    microseconds: int,
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
    action: ScheduledAction,
    intent: OrderIntent,
) -> None:
    # Los campos temporales difieren para detectar el uso de un origen causal incorrecto.
    quote = replace(quote, timestamp=action.created_at + timedelta(minutes=3))
    order = replace(order, updated_at=order.created_at + timedelta(minutes=2))
    fill = replace(fill, timestamp=action.created_at + timedelta(minutes=4))
    event_time = causal_time(handler, candle, quote, order, fill)
    created_at = event_time + timedelta(microseconds=microseconds)
    new_action = replace(
        action, action_id=ActionId(UUID(int=22)), created_at=created_at, not_before=created_at
    )
    new_intent = replace(intent, created_at=created_at)
    response = StrategyResult(
        scheduled_actions=(new_action,) if output_kind == "action" else (),
        order_intents=(new_intent,) if output_kind == "intent" else (),
    )
    strategy = CannedStrategy((response,))
    state = CoordinatorState((action,))  # Una acción preexistente puede ser anterior al evento.
    arguments = event_arguments(handler, candle, quote, order, fill, action)
    coordinator = StrategyCoordinator(strategy)
    if microseconds < 0:
        with pytest.raises(ValueError, match="created_at.*event_time"):
            getattr(coordinator, handler)(state, **arguments)
    else:
        result = getattr(coordinator, handler)(state, **arguments)
        assert result.order_intents == response.order_intents
        expected_actions = (action, new_action) if output_kind == "action" else (action,)
        assert result.state.pending_actions == expected_actions
    assert strategy.calls == [(handler, (*arguments.values(), None, None))]
    assert state.pending_actions == (action,)
    assert new_action.created_at == created_at
    assert new_intent.created_at == created_at  # No corregir timestamps automáticamente.


def test_fill_observed_before_order_creation_still_uses_fill_event_time(
    order: Order,
    fill: Fill,
    intent: OrderIntent,
    action: ScheduledAction,
) -> None:
    order = replace(
        order,
        created_at=order.created_at + timedelta(minutes=3),
        updated_at=order.updated_at + timedelta(minutes=4),
    )
    assert fill.timestamp < order.created_at
    new_action = replace(
        action,
        action_id=ActionId(UUID(int=22)),
        created_at=fill.timestamp,
        not_before=fill.timestamp,
    )
    new_intent = replace(intent, created_at=fill.timestamp)
    response = StrategyResult(scheduled_actions=(new_action,), order_intents=(new_intent,))
    strategy = CannedStrategy((response,))
    result = StrategyCoordinator(strategy).on_fill(CoordinatorState(), fill, order)
    assert result.state.pending_actions == (new_action,)
    assert result.order_intents == (new_intent,)
    assert strategy.calls == [("on_fill", (fill, order, None, None))]
