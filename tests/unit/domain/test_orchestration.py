import ast
import time
import uuid
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from itertools import permutations
from pathlib import Path
from typing import Self, cast
from uuid import UUID

import pytest

import trading_engine.orchestration.coordinator as coordinator_module
from trading_engine.models.account import Position
from trading_engine.models.decisions import StrategyResult
from trading_engine.models.ids import IntentId, SignalId
from trading_engine.models.market import Candle
from trading_engine.models.orders import OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.signals import Signal
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import (
    CoordinationResult,
    CoordinatorState,
    DueIntentsResult,
)


class CannedStrategy:
    def __init__(self, responses: tuple[StrategyResult, ...]) -> None:
        self.responses = responses
        self.calls: list[tuple[Candle, Position | None, Round | None]] = []

    def on_candle(
        self, candle: Candle, position: Position | None, round_state: Round | None
    ) -> StrategyResult:
        index = len(self.calls)
        self.calls.append((candle, position, round_state))
        return self.responses[index]


def scheduled(intent: OrderIntent, identifier: int, seconds: int) -> OrderIntent:
    return replace(
        intent,
        intent_id=IntentId(UUID(int=identifier)),
        not_before=intent.created_at + timedelta(seconds=seconds),
    )


def test_empty_state_and_time_advance() -> None:
    state = CoordinatorState()
    strategy = CannedStrategy(())
    result = StrategyCoordinator(strategy).advance_time(state, datetime(2026, 1, 1, tzinfo=UTC))
    assert result == DueIntentsResult((), CoordinatorState())
    assert strategy.calls == []


def test_state_canonicalizes_eligibility_then_uuid(intent: OrderIntent) -> None:
    early = scheduled(intent, 20, 0)
    low_id = scheduled(intent, 2, 17)
    high_id = scheduled(intent, 10, 17)
    expected = (early, low_id, high_id)
    for insertion_order in permutations(expected):
        state = CoordinatorState(insertion_order)
        assert state.pending_intents == expected
    assert CoordinatorState((intent,)).pending_intents == (intent,)


@pytest.mark.parametrize("pending", [[], None, ("invalid",)])
def test_state_rejects_wrong_types(pending: object) -> None:
    with pytest.raises(TypeError):
        CoordinatorState(cast(tuple[OrderIntent, ...], pending))


def test_state_rejects_duplicate_ids_even_if_other_fields_differ(intent: OrderIntent) -> None:
    with pytest.raises(ValueError, match="intent_id duplicado"):
        CoordinatorState((intent, replace(intent, reduce_only=True)))


def test_state_and_results_are_immutable(intent: OrderIntent) -> None:
    state = CoordinatorState((intent,))
    coordination = CoordinationResult((), state)
    due = DueIntentsResult((intent,), CoordinatorState())
    with pytest.raises(FrozenInstanceError):
        state.pending_intents = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        coordination.signals = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        coordination.state = CoordinatorState()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        due.due_intents = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        due.state = state  # type: ignore[misc]


@pytest.mark.parametrize("changes", [{"signals": []}, {"signals": ("invalid",)}, {"state": None}])
def test_coordination_result_rejects_invalid_types(changes: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        replace(CoordinationResult((), CoordinatorState()), **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes", [{"due_intents": []}, {"due_intents": ("invalid",)}, {"state": None}]
)
def test_due_result_rejects_invalid_types(changes: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        replace(DueIntentsResult((), CoordinatorState()), **changes)  # type: ignore[arg-type]


def test_due_result_canonicalizes_and_rejects_duplicates(intent: OrderIntent) -> None:
    earlier = scheduled(intent, 2, 0)
    later = scheduled(intent, 3, 7)
    assert DueIntentsResult((later, earlier), CoordinatorState()).due_intents == (earlier, later)
    with pytest.raises(ValueError):
        DueIntentsResult((intent, intent), CoordinatorState())


def test_on_candle_calls_strategy_once_preserves_signals_and_only_schedules(
    intent: OrderIntent, candle: Candle, position: Position, round_state: Round
) -> None:
    existing = scheduled(intent, 20, 5)
    immediate = scheduled(intent, 2, 0)
    deferred = scheduled(intent, 3, 31)
    signals = (
        Signal(SignalId(UUID(int=8)), candle.symbol, candle.close_time, "second"),
        Signal(SignalId(UUID(int=7)), candle.symbol, candle.close_time, "first"),
    )
    strategy = CannedStrategy((StrategyResult(signals, (deferred, immediate)),))
    state = CoordinatorState((existing,))
    result = StrategyCoordinator(strategy).on_candle(state, candle, position, round_state)
    assert strategy.calls == [(candle, position, round_state)]
    assert result.signals is signals
    assert result.state.pending_intents == (immediate, existing, deferred)
    assert state.pending_intents == (existing,)
    assert result.state is not state


def test_signal_alone_does_not_create_intent_or_round(candle: Candle) -> None:
    signal = Signal(SignalId(UUID(int=7)), candle.symbol, candle.close_time, "observation")
    strategy = CannedStrategy((StrategyResult(signals=(signal,)),))
    result = StrategyCoordinator(strategy).on_candle(CoordinatorState(), candle)
    assert result.signals == (signal,)
    assert result.state == CoordinatorState()
    assert strategy.calls == [(candle, None, None)]


@pytest.mark.parametrize("duplicate_existing", [False, True])
def test_on_candle_rejects_duplicate_ids(
    intent: OrderIntent, candle: Candle, duplicate_existing: bool
) -> None:
    state = CoordinatorState((intent,)) if duplicate_existing else CoordinatorState()
    produced = (intent,) if duplicate_existing else (intent, intent)
    strategy = CannedStrategy((StrategyResult(order_intents=produced),))
    with pytest.raises(ValueError, match="intent_id duplicado"):
        StrategyCoordinator(strategy).on_candle(state, candle)
    assert len(strategy.calls) == 1
    assert state.pending_intents == ((intent,) if duplicate_existing else ())


def test_on_candle_empty_result_preserves_pending(intent: OrderIntent, candle: Candle) -> None:
    state = CoordinatorState((intent,))
    strategy = CannedStrategy((StrategyResult(),))
    result = StrategyCoordinator(strategy).on_candle(state, candle)
    assert result.signals == () and result.state == state
    assert len(strategy.calls) == 1


def test_bad_strategy_result_and_errors_are_not_hidden(candle: Candle) -> None:
    invalid = CannedStrategy((cast(StrategyResult, ()),))
    with pytest.raises(TypeError, match="strategy_result"):
        StrategyCoordinator(invalid).on_candle(CoordinatorState(), candle)
    assert len(invalid.calls) == 1
    failing = CannedStrategy(())
    with pytest.raises(IndexError):
        StrategyCoordinator(failing).on_candle(CoordinatorState(), candle)
    assert len(failing.calls) == 1


@pytest.mark.parametrize("seconds, eligible", [(0, False), (119, False), (120, True), (180, True)])
def test_explicit_1000_to_1002_boundary(intent: OrderIntent, seconds: int, eligible: bool) -> None:
    created = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    deferred = replace(intent, created_at=created, not_before=created.replace(minute=2))
    state = CoordinatorState((deferred,))
    strategy = CannedStrategy(())
    coordinator = StrategyCoordinator(strategy)
    result = coordinator.advance_time(state, created + timedelta(seconds=seconds))
    assert result.due_intents == ((deferred,) if eligible else ())
    assert result.state.pending_intents == (() if eligible else (deferred,))
    assert state.pending_intents == (deferred,)
    assert strategy.calls == []
    if eligible:
        assert (
            coordinator.advance_time(result.state, created + timedelta(seconds=181)).due_intents
            == ()
        )


def test_advance_partitions_multiple_intents_in_canonical_order(intent: OrderIntent) -> None:
    early = scheduled(intent, 8, 0)
    tied_low = scheduled(intent, 2, 7)
    tied_high = replace(scheduled(intent, 9, 7), reduce_only=True)
    later = scheduled(intent, 1, 8)
    state = CoordinatorState((later, tied_high, early, tied_low))
    coordinator = StrategyCoordinator(CannedStrategy(()))
    result = coordinator.advance_time(state, intent.created_at + timedelta(seconds=7))
    assert result.due_intents == (early, tied_low, tied_high)
    assert result.due_intents[-1] is tied_high
    assert result.due_intents[-1].reduce_only is True
    assert result.state.pending_intents == (later,)
    final = coordinator.advance_time(result.state, intent.created_at + timedelta(seconds=8))
    assert final.due_intents == (later,) and final.state == CoordinatorState()


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


def test_repeating_the_same_sequence_is_deterministic(intent: OrderIntent, candle: Candle) -> None:
    early = scheduled(intent, 2, 7)
    late = scheduled(intent, 3, 19)
    signals = (
        Signal(SignalId(UUID(int=6)), candle.symbol, candle.close_time, "b"),
        Signal(SignalId(UUID(int=5)), candle.symbol, candle.close_time, "a"),
    )
    responses = (StrategyResult(signals, (late, early)), StrategyResult())

    def run() -> tuple[CoordinationResult, DueIntentsResult, CoordinationResult, DueIntentsResult]:
        coordinator = StrategyCoordinator(CannedStrategy(responses))
        first = coordinator.on_candle(CoordinatorState(), candle)
        due = coordinator.advance_time(first.state, intent.created_at + timedelta(seconds=7))
        second = coordinator.on_candle(due.state, candle)
        final = coordinator.advance_time(second.state, intent.created_at + timedelta(seconds=19))
        return first, due, second, final

    assert run() == run()


def test_coordinator_uses_no_system_clock_or_generated_ids(
    monkeypatch: pytest.MonkeyPatch, intent: OrderIntent, candle: Candle
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
    coordinator = StrategyCoordinator(CannedStrategy((StrategyResult(order_intents=(intent,)),)))
    result = coordinator.on_candle(CoordinatorState(), candle)
    assert coordinator.advance_time(result.state, intent.not_before).due_intents == (intent,)


@pytest.mark.parametrize(
    "changes",
    [{"state": None}, {"candle": "invalid"}, {"position": "invalid"}, {"round_state": "invalid"}],
)
def test_on_candle_invalid_arguments_fail_before_strategy(
    candle: Candle, changes: dict[str, object]
) -> None:
    strategy = CannedStrategy((StrategyResult(),))
    arguments: dict[str, object] = {"state": CoordinatorState(), "candle": candle, **changes}
    with pytest.raises(TypeError):
        StrategyCoordinator(strategy).on_candle(**arguments)  # type: ignore[arg-type]
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
