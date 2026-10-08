from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Context, Decimal, Inexact, localcontext
from typing import cast

import pytest

from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.strategy.alt_b.market import aggregate_15m_block, decimal_median, rv30m_snapshot


@pytest.mark.parametrize("offset", [0, 15, 30, 45])
def test_aggregation_all_quarter_hours_and_exact_ohlc(
    history: tuple[Candle, ...], offset: int
) -> None:
    block = history[offset : offset + 15]
    enriched = tuple(
        replace(c, volume=Decimal(i + 1), taker_buy_base_volume=Decimal(i + 1) / 2)
        for i, c in enumerate(block)
    )
    enriched = (replace(enriched[0], open=Price(Decimal("102"))), *enriched[1:])
    enriched = (
        *enriched[:7],
        replace(enriched[7], high=Price(Decimal("110")), low=Price(Decimal("90"))),
        *enriched[8:],
    )
    result = aggregate_15m_block(enriched)
    assert result.open_time == enriched[0].open_time
    assert result.close_time == enriched[0].open_time + timedelta(minutes=15)
    assert result.open == Price(Decimal("102"))
    assert result.high == Price(Decimal("110"))
    assert result.low == Price(Decimal("90"))
    assert result.close == enriched[-1].close
    assert result.volume == Decimal("120")
    assert result.taker_buy_base_volume == Decimal("60")


@pytest.mark.parametrize("missing", [0, 7, 14])
def test_aggregation_preserves_missing_taker_buy(history: tuple[Candle, ...], missing: int) -> None:
    block = tuple(
        replace(c, taker_buy_base_volume=None) if i == missing else c
        for i, c in enumerate(history[:15])
    )
    result = aggregate_15m_block(block)
    assert result.taker_buy_base_volume is None
    assert result.volume == Decimal("15")


@pytest.mark.parametrize("length", [0, 14, 16])
def test_aggregation_requires_exactly_fifteen(history: tuple[Candle, ...], length: int) -> None:
    with pytest.raises(ValueError):
        aggregate_15m_block(history[:length])


@pytest.mark.parametrize(
    "defect",
    [
        "symbol",
        "gap",
        "duplicate",
        "5m",
        "seconds",
        "microseconds",
        "provider_end",
        "quarter",
        "reversed",
    ],
)
def test_aggregation_rejects_temporal_defects(history: tuple[Candle, ...], defect: str) -> None:
    block = list(history[:15])
    if defect == "symbol":
        block[7] = replace(block[7], symbol=Symbol("OTHER"))
    elif defect == "gap":
        block[7] = history[16]
    elif defect == "duplicate":
        block[7] = block[6]
    elif defect == "5m":
        block[7] = replace(block[7], close_time=block[7].open_time + timedelta(minutes=5))
    elif defect in {"seconds", "microseconds", "quarter"}:
        shift = {
            "seconds": timedelta(seconds=1),
            "microseconds": timedelta(microseconds=1),
            "quarter": timedelta(minutes=1),
        }[defect]
        block = [
            replace(c, open_time=c.open_time + shift, close_time=c.close_time + shift)
            for c in block
        ]
    elif defect == "provider_end":
        block[7] = replace(block[7], close_time=block[7].close_time - timedelta(milliseconds=1))
    else:
        block.reverse()
    with pytest.raises(ValueError):
        aggregate_15m_block(tuple(block))


@pytest.mark.parametrize("candles", [[], (None,), "candles"])
def test_aggregation_requires_tuple_of_candles(candles: object) -> None:
    with pytest.raises(TypeError):
        aggregate_15m_block(cast(tuple[Candle, ...], candles))


@pytest.mark.parametrize(
    "values,expected",
    [
        (("9", "1", "3"), "3"),
        (("9", "1", "3", "5"), "4"),
        (("1.00001", "1.00002"), "1.000015"),
        (("-2", "-1"), "-1.5"),
    ],
)
def test_decimal_median_odd_even_and_exact(values: tuple[str, ...], expected: str) -> None:
    data = tuple(map(Decimal, values))
    assert decimal_median(data) == Decimal(expected)
    assert data == tuple(map(Decimal, values))


@pytest.mark.parametrize(
    "values,error",
    [
        ((), ValueError),
        ([], TypeError),
        ((1,), TypeError),
        ((True,), TypeError),
        ((0.1,), TypeError),
        ((Decimal("NaN"),), ValueError),
    ],
)
def test_decimal_median_rejects_invalid_input(values: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        decimal_median(cast(tuple[Decimal, ...], values))


def test_rv_uses_thirty_log_returns_population_variance_and_no_annualization(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    window = tuple(
        replace(c, close=Price(Decimal("100") + Decimal((i * i + 7 * i) % 17) / 10))
        for i, c in enumerate(history[-31:])
    )
    with localcontext(Context(prec=100)):
        prices = tuple(c.close.value for c in window)
        log_returns = tuple(prices[i].ln() - prices[i - 1].ln() for i in range(1, 31))
        mean = sum(log_returns, Decimal("0")) / 30
        variance = sum(((r - mean) ** 2 for r in log_returns), Decimal("0")) / 30
        expected = variance.sqrt()
        sample = (sum(((r - mean) ** 2 for r in log_returns), Decimal("0")) / 29).sqrt()
        simple = tuple(prices[i] / prices[i - 1] - 1 for i in range(1, 31))
        simple_mean = sum(simple, Decimal("0")) / 30
        simple_rv = (sum(((r - simple_mean) ** 2 for r in simple), Decimal("0")) / 30).sqrt()
        actual = rv30m_snapshot(window, checkpoint)
        assert actual is not None
        assert abs(actual - expected) < Decimal("1e-48")
        assert abs(actual - sample) > Decimal("1e-6")
        assert abs(actual - simple_rv) > Decimal("1e-8")
        assert abs(actual - expected * Decimal("30").sqrt()) > Decimal("0.001")


@pytest.mark.parametrize(
    "defect", ["30_closes", "gap", "duplicate", "misaligned", "missing_checkpoint", "symbol"]
)
def test_rv_snapshot_unavailable_for_missing_continuity(
    history: tuple[Candle, ...], checkpoint: datetime, defect: str
) -> None:
    window = list(history[-31:])
    if defect == "30_closes":
        window = window[1:]
    elif defect == "gap":
        del window[10]
    elif defect == "duplicate":
        window[10] = window[9]
    elif defect == "missing_checkpoint":
        window = window[:-1]
    elif defect == "symbol":
        window[10] = replace(window[10], symbol=Symbol("OTHER"))
    else:
        window[10] = replace(window[10], open_time=window[10].open_time + timedelta(seconds=1))
    assert rv30m_snapshot(tuple(window), checkpoint) is None


def test_rv_requires_quarter_hour_and_rejects_future(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    with pytest.raises(ValueError):
        rv30m_snapshot(history[-31:], checkpoint - timedelta(minutes=1))
    future = replace(
        history[-1], open_time=checkpoint, close_time=checkpoint + timedelta(minutes=1)
    )
    with pytest.raises(ValueError):
        rv30m_snapshot((*history[-31:], future), checkpoint)


def test_market_math_is_independent_of_ambient_context(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    block = history[-15:]
    values = (Decimal("1.000001"), Decimal("2.000002"))
    baseline = (
        aggregate_15m_block(block),
        decimal_median(values),
        rv30m_snapshot(history[-31:], checkpoint),
    )
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.rounding = ROUND_UP
        ambient.traps[Inexact] = True
        ambient.clear_flags()
        before = ambient.copy()
        assert (
            aggregate_15m_block(block),
            decimal_median(values),
            rv30m_snapshot(history[-31:], checkpoint),
        ) == baseline
        assert ambient.prec == before.prec and ambient.rounding == before.rounding
        assert ambient.flags == before.flags and ambient.traps == before.traps
