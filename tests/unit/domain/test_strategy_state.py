"""Transferencia opaca y cancelación genérica sin dependencia de Alt B."""

from dataclasses import FrozenInstanceError, dataclass, fields, replace
from typing import cast
from uuid import UUID

import pytest
from test_orchestration import CannedStrategy

from trading_engine.models.account import Position
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyResult, StrategyState
from trading_engine.models.ids import ActionId
from trading_engine.models.market import Quote
from trading_engine.models.rounds import Round
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinatorState


@dataclass(frozen=True)
class SampleState(StrategyState):
    count: int = 0


def test_coordinator_passes_and_replaces_explicit_strategy_state(quote: Quote) -> None:
    previous = SampleState(5)
    updated = SampleState(6)

    class StatefulFake(CannedStrategy):
        def on_quote(
            self,
            quote: Quote,
            position: Position | None,
            round_state: Round | None,
            *,
            strategy_state: StrategyState | None = None,
        ) -> StrategyResult:
            assert strategy_state is previous
            return StrategyResult(strategy_state=updated)

    original = CoordinatorState(strategy_state=previous)
    result = StrategyCoordinator(StatefulFake(())).on_quote(original, quote)
    assert result.state.strategy_state is updated and original.strategy_state is previous


def test_empty_legacy_result_preserves_state_and_pending(
    action: ScheduledAction, quote: Quote
) -> None:
    state = CoordinatorState((action,), SampleState(5))
    result = StrategyCoordinator(CannedStrategy((StrategyResult(),))).on_quote(state, quote)
    assert result.state == state


def test_cancellation_removes_only_named_actions_and_retains_strategy_state(
    action: ScheduledAction, quote: Quote
) -> None:
    other = replace(action, action_id=ActionId(UUID(int=300)))
    state = CoordinatorState((action, other), SampleState(5))
    strategy = CannedStrategy((StrategyResult(cancelled_action_ids=(action.action_id,)),))
    result = StrategyCoordinator(strategy).on_quote(state, quote)
    assert result.state.pending_actions == (other,) and result.state.strategy_state == SampleState(
        5
    )
    assert len(state.pending_actions) == 2


def test_due_extraction_preserves_explicit_state(action: ScheduledAction) -> None:
    snapshot = SampleState(5)
    state = CoordinatorState((action,), snapshot)
    result = StrategyCoordinator(CannedStrategy(())).advance_time(state, action.not_before)
    assert result.due_actions == (action,) and result.state.strategy_state is snapshot


@pytest.mark.parametrize("value", [[], ("wrong",), (1,), None])
def test_result_rejects_invalid_cancellation_container_or_ids(value: object) -> None:
    with pytest.raises(TypeError):
        StrategyResult(cancelled_action_ids=cast(tuple[ActionId, ...], value))


def test_result_rejects_duplicate_or_conflicting_cancellation(action: ScheduledAction) -> None:
    with pytest.raises(ValueError, match="duplicado"):
        StrategyResult(cancelled_action_ids=(action.action_id, action.action_id))
    with pytest.raises(ValueError, match="misma acción"):
        StrategyResult(scheduled_actions=(action,), cancelled_action_ids=(action.action_id,))


def test_state_contract_is_frozen_and_rejects_untyped_mutable_objects() -> None:
    for snapshot in (
        SampleState(),
        StrategyResult(strategy_state=SampleState()),
        CoordinatorState(strategy_state=SampleState()),
    ):
        for field in fields(snapshot):
            with pytest.raises(FrozenInstanceError):
                setattr(snapshot, field.name, None)
    for factory in (StrategyResult, CoordinatorState):
        with pytest.raises(TypeError):
            factory(strategy_state=cast(StrategyState, {}))


def test_control_and_invalidation_validate_before_calling_strategy(quote: Quote) -> None:
    strategy = CannedStrategy(())
    coordinator = StrategyCoordinator(strategy)
    with pytest.raises(TypeError):
        coordinator.on_entry_control(CoordinatorState(), cast(bool, 1), quote.timestamp)
    with pytest.raises(ValueError):
        coordinator.on_entry_control(
            CoordinatorState(), False, quote.timestamp.replace(tzinfo=None)
        )
    with pytest.raises(TypeError):
        coordinator.invalidate_action(CoordinatorState(), cast(ActionId, "wrong"), quote.timestamp)
    assert not strategy.calls


def test_control_and_invalidation_dispatch_only_once(quote: Quote) -> None:
    strategy = CannedStrategy((StrategyResult(), StrategyResult()))
    coordinator = StrategyCoordinator(strategy)
    result = coordinator.on_entry_control(CoordinatorState(), False, quote.timestamp)
    coordinator.invalidate_action(result.state, ActionId(UUID(int=5)), quote.timestamp)
    assert [method for method, _ in strategy.calls] == ["control", "invalidate"]
