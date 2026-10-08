from dataclasses import FrozenInstanceError, replace
from itertools import product
from uuid import UUID

import pytest

from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyResult
from trading_engine.models.ids import SignalId
from trading_engine.models.orders import OrderIntent
from trading_engine.models.signals import Signal


def test_strategy_result_defaults_to_empty() -> None:
    result = StrategyResult()
    assert result.signals == ()
    assert result.scheduled_actions == ()
    assert result.order_intents == ()


@pytest.mark.parametrize(
    "has_signal, has_action, has_intent", list(product([False, True], repeat=3))
)
def test_strategy_result_supports_three_independent_outputs(
    intent: OrderIntent,
    action: ScheduledAction,
    has_signal: bool,
    has_action: bool,
    has_intent: bool,
) -> None:
    signal = Signal(SignalId(UUID(int=5)), intent.symbol, intent.created_at, "observation")
    signals = (signal,) if has_signal else ()
    actions = (action,) if has_action else ()
    intents = (intent,) if has_intent else ()
    result = StrategyResult(signals=signals, scheduled_actions=actions, order_intents=intents)
    assert result.signals == signals
    assert result.scheduled_actions == actions
    assert result.order_intents == intents
    for field in ("signals", "scheduled_actions", "order_intents"):
        with pytest.raises(FrozenInstanceError):
            setattr(result, field, ())


@pytest.mark.parametrize("field", ["signals", "scheduled_actions", "order_intents"])
@pytest.mark.parametrize("value", [[], None, ("invalid",)])
def test_strategy_result_rejects_mutable_or_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        replace(StrategyResult(), **{field: value})  # type: ignore[arg-type]


def test_strategy_result_rejects_crossed_output_types(
    action: ScheduledAction,
    intent: OrderIntent,
) -> None:
    signal = Signal(SignalId(UUID(int=5)), intent.symbol, intent.created_at, "observation")
    for field, value in (
        ("signals", action),
        ("signals", intent),
        ("scheduled_actions", signal),
        ("scheduled_actions", intent),
        ("order_intents", signal),
        ("order_intents", action),
    ):
        with pytest.raises(TypeError):
            replace(StrategyResult(), **{field: (value,)})  # type: ignore[arg-type]
