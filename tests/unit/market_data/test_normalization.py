from datetime import UTC, timedelta
from decimal import Decimal, Inexact, localcontext

import pytest

from trading_engine.binance.klines import (
    DEFAULT_SYMBOL,
    LAUNCH,
    from_ms,
    minute_ms,
    normalize_kline,
    to_ms,
)
from trading_engine.models.values import Symbol

from .conftest import row_at

AS_OF = LAUNCH + timedelta(days=1)


def test_exact_decimal_taker_and_exclusive_close() -> None:
    row = row_at()
    candle = normalize_kline(row, DEFAULT_SYMBOL, as_of=AS_OF)
    assert candle.symbol == DEFAULT_SYMBOL
    assert candle.open_time == LAUNCH
    assert candle.close_time == LAUNCH + timedelta(minutes=1)
    assert candle.close_time.tzinfo is UTC
    assert candle.open.value == Decimal(str(row[1]))
    assert candle.volume == Decimal(str(row[5]))
    assert candle.taker_buy_base_volume == Decimal(str(row[9]))
    assert candle.taker_buy_base_volume != Decimal(str(row[10]))
    assert isinstance(candle.open.value, Decimal)


def test_parsing_independent_of_global_decimal_precision() -> None:
    expected = normalize_kline(row_at(), DEFAULT_SYMBOL, as_of=AS_OF)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        before = context.copy()
        assert normalize_kline(row_at(), DEFAULT_SYMBOL, as_of=AS_OF) == expected
        assert context.flags == before.flags and context.traps == before.traps


@pytest.mark.parametrize(
    "index,value",
    [
        (0, True),
        (0, 1711609200000000),
        (0, 1711609200),
        (0, "1.0"),
        (0, to_ms(LAUNCH) + 1),
        (6, to_ms(LAUNCH) + 60000),
        (6, to_ms(LAUNCH) + 59998),
        (1, 1.1),
        (1, 1),
        (1, "0"),
        (2, "98"),
        (3, "102"),
        (4, "-1"),
        (5, "-1"),
        (5, "Infinity"),
        (9, "NaN"),
        (9, "-1"),
        (9, "11"),
        (9, None),
        (9, ""),
        (9, " 1"),
        (7, "NaN"),
        (8, True),
        (8, -1),
        (8, "2.0"),
        (10, "3000"),
        (11, "invalid"),
    ],
)
def test_invalid_fields_rejected(index: int, value: object) -> None:
    row = row_at()
    row[index] = value
    with pytest.raises(ValueError):
        normalize_kline(row, DEFAULT_SYMBOL, as_of=AS_OF)


@pytest.mark.parametrize("length", [0, 11, 13])
def test_exactly_twelve_fields(length: int) -> None:
    row = row_at() + ["0"]
    with pytest.raises(ValueError):
        normalize_kline(row[:length], DEFAULT_SYMBOL, as_of=AS_OF)


@pytest.mark.parametrize("symbol", ["SHIBUSDC", "SHIBUSDT", "1000SHIBUSDT", "BAD/PATH"])
def test_prohibited_or_unsafe_symbol(symbol: str) -> None:
    with pytest.raises(ValueError):
        normalize_kline(row_at(), Symbol(symbol), as_of=AS_OF)


def test_rejects_open_future_and_prelaunch() -> None:
    for at in (LAUNCH, LAUNCH + timedelta(seconds=59, milliseconds=999)):
        with pytest.raises(ValueError, match="abierta"):
            normalize_kline(row_at(), DEFAULT_SYMBOL, as_of=at)
    with pytest.raises(ValueError, match="lanzamiento"):
        normalize_kline(row_at(-1), DEFAULT_SYMBOL, as_of=AS_OF)
    assert (
        normalize_kline(row_at(), DEFAULT_SYMBOL, as_of=LAUNCH + timedelta(minutes=1)).close_time
        > LAUNCH
    )


def test_reference_clock_microseconds_do_not_change_closed_minute_boundary() -> None:
    as_of = LAUNCH + timedelta(minutes=1, microseconds=1)
    assert normalize_kline(row_at(), DEFAULT_SYMBOL, as_of=as_of).close_time < as_of
    with pytest.raises(ValueError, match="abierta"):
        normalize_kline(
            row_at(), DEFAULT_SYMBOL, as_of=LAUNCH + timedelta(seconds=59, microseconds=999999)
        )


@pytest.mark.parametrize("value", [True, 1711609200000000, 1711609200, -1])
def test_rejects_unexpected_ms_units(value: int) -> None:
    with pytest.raises(ValueError):
        from_ms(value)


def test_utc_and_exact_millisecond_boundaries() -> None:
    assert from_ms(to_ms(LAUNCH)) == LAUNCH
    assert minute_ms(LAUNCH) == to_ms(LAUNCH)
    for at in (LAUNCH.replace(tzinfo=None), LAUNCH + timedelta(microseconds=1)):
        with pytest.raises(ValueError):
            to_ms(at)
    with pytest.raises(ValueError):
        minute_ms(LAUNCH + timedelta(seconds=1))
    with pytest.raises(ValueError):
        normalize_kline(row_at(), DEFAULT_SYMBOL, as_of=LAUNCH.replace(tzinfo=None))
