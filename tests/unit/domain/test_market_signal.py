from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from trading_engine.models.ids import SignalId
from trading_engine.models.market import Candle
from trading_engine.models.signals import Signal
from trading_engine.models.values import Price, Symbol


def test_candle_has_no_fixed_interval_and_allows_zero_volume(candle: Candle) -> None:
    assert candle.close_time - candle.open_time == timedelta(minutes=5)
    assert candle.volume == 0
    assert replace(candle, close_time=candle.open_time + timedelta(seconds=1))
    assert replace(candle, open=candle.low, close=candle.high)
    with pytest.raises(FrozenInstanceError):
        candle.volume = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"high": Price(Decimal("1.5"))},
        {"high": Price(Decimal("2.1"))},
        {"low": Price(Decimal("2.1"))},
        {"low": Price(Decimal("3.1"))},
        {"open": Price(Decimal("4"))},
        {"close": Price(Decimal("0.5"))},
    ],
)
def test_candle_rejects_inconsistent_ohlc(candle: Candle, changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(candle, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_candle_rejects_float(candle: Candle, field: str) -> None:
    with pytest.raises(TypeError):
        replace(candle, **{field: 1.0})  # type: ignore[arg-type]


@pytest.mark.parametrize("volume", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_candle_rejects_invalid_volume(candle: Candle, volume: Decimal) -> None:
    with pytest.raises(ValueError):
        replace(candle, volume=volume)


@pytest.mark.parametrize("field", ["open_time", "close_time"])
@pytest.mark.parametrize(
    "invalid",
    [
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_candle_rejects_invalid_timestamps(candle: Candle, field: str, invalid: datetime) -> None:
    with pytest.raises(ValueError):
        replace(candle, **{field: invalid})  # type: ignore[arg-type]


@pytest.mark.parametrize("offset", [0, -1])
def test_candle_requires_strict_time_order(candle: Candle, offset: int) -> None:
    with pytest.raises(ValueError):
        replace(candle, close_time=candle.open_time + timedelta(seconds=offset))


def test_minimal_signal(timestamp: datetime) -> None:
    signal = Signal(SignalId(UUID(int=1)), Symbol("SHIBUSDC"), timestamp, "external-observation")
    assert signal.timestamp == timestamp
    with pytest.raises(FrozenInstanceError):
        signal.kind = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"signal_id": "arbitrary"}, TypeError),
        ({"symbol": "SHIBUSDC"}, TypeError),
        ({"timestamp": datetime(2026, 1, 1)}, ValueError),
        ({"timestamp": "2026-01-01"}, TypeError),
        ({"kind": " "}, ValueError),
    ],
)
def test_signal_rejects_invalid_fields(
    timestamp: datetime, changes: dict[str, object], error: type[Exception]
) -> None:
    signal = Signal(SignalId(UUID(int=1)), Symbol("SHIBUSDC"), timestamp, "observation")
    with pytest.raises(error):
        replace(signal, **changes)  # type: ignore[arg-type]
