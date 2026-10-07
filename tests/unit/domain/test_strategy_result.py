from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest

from trading_engine.models.decisions import StrategyResult
from trading_engine.models.ids import SignalId
from trading_engine.models.orders import OrderIntent
from trading_engine.models.signals import Signal


def test_strategy_result_defaults_to_empty() -> None:
    result = StrategyResult()
    assert result.signals == ()
    assert result.order_intents == ()


@pytest.mark.parametrize(
    "has_signal, has_intent", [(False, False), (True, False), (False, True), (True, True)]
)
def test_strategy_result_supports_both_outputs(
    intent: OrderIntent, has_signal: bool, has_intent: bool
) -> None:
    signal = Signal(SignalId(UUID(int=5)), intent.symbol, intent.created_at, "observation")
    signals = (signal,) if has_signal else ()
    intents = (intent,) if has_intent else ()
    result = StrategyResult(signals=signals, order_intents=intents)
    assert result.signals == signals
    assert result.order_intents == intents
    with pytest.raises(FrozenInstanceError):
        result.signals = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.order_intents = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"signals": []},
        {"order_intents": []},
        {"signals": None},
        {"order_intents": None},
        {"signals": ("invalid",)},
        {"order_intents": ("invalid",)},
    ],
)
def test_strategy_result_rejects_mutable_or_invalid_fields(changes: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        replace(StrategyResult(), **changes)  # type: ignore[arg-type]
