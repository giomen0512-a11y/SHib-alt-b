from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Context, Decimal, Inexact, Rounded, localcontext
from typing import cast
from uuid import UUID

import pytest

from trading_engine.models.ids import FillId, OrderId
from trading_engine.models.orders import Fill
from trading_engine.models.values import Price, Quantity
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.math import (
    MATH_PRECISION,
    dca_cumulative_deviation,
    dca_theoretical_price,
    order_margins,
    order_notionals,
    order_weights,
    take_profit_theoretical_price,
    weighted_average_fill_price,
)

CONFIG = AltBConfig()
MARGINS_GOLDEN = (
    "41.527777756923",
    "48.566384156401",
    "67.992937818961",
    "95.190112946546",
    "133.266158125165",
    "186.572621375230",
    "261.201669925322",
    "365.682337895451",
)
DEVIATIONS = (
    "0.01500000",
    "0.03225000",
    "0.05208750",
    "0.074900625",
    "0.10113571875",
    "0.1313060765625",
    "0.166001988046875",
)


def make_fill(price: str, quantity: str, index: int = 1) -> Fill:
    return Fill(
        FillId(UUID(int=index)),
        OrderId(UUID(int=100)),
        datetime(2026, 1, 1, tzinfo=UTC),
        Price(Decimal(price)),
        Quantity(Decimal(quantity)),
    )


def test_weights_and_total_are_exact_canonical_values() -> None:
    weights = order_weights(CONFIG)
    assert isinstance(weights, tuple)
    assert weights == tuple(
        map(
            Decimal,
            ("59", "69", "96.6", "135.24", "189.336", "265.0704", "371.09856", "519.537984"),
        )
    )
    assert sum(weights) == Decimal("1704.882944")
    assert weights[0] == CONFIG.base_weight
    for level in range(1, 8):
        assert weights[level] == CONFIG.dca1_weight * CONFIG.amount_multiplier ** (level - 1)
    assert all(left < right for left, right in zip(weights[1:], weights[2:], strict=False))


def test_margins_match_independent_high_precision_golden_and_cents_presentation() -> None:
    margins = order_margins(CONFIG)
    assert isinstance(margins, tuple) and len(margins) == 8
    with localcontext(Context(prec=80)):
        assert all(
            abs(actual - Decimal(golden)) <= Decimal("0.0000000000005")
            for actual, golden in zip(margins, MARGINS_GOLDEN, strict=True)
        )
        assert tuple(value.quantize(Decimal("0.01")) for value in margins) == tuple(
            map(
                Decimal,
                ("41.53", "48.57", "67.99", "95.19", "133.27", "186.57", "261.20", "365.68"),
            )
        )
        assert all(value != value.quantize(Decimal("0.01")) for value in margins)
        # Referencia independiente a 80 dígitos, sin reutilizar pesos de producción.
        expected = Decimal("1200") * Decimal("519.537984") / Decimal("1704.882944")
        tolerance = Decimal("1").scaleb(expected.adjusted() - MATH_PRECISION + 1)
        assert abs(margins[-1] - expected) <= tolerance / 2


@pytest.mark.parametrize(
    "budget", ["0.000000001", "1e-1000", "1", "1200", "2000", "3200", "1234.56789", "1e1000"]
)
def test_budget_conservation_linear_scaling_and_notional_properties(budget: str) -> None:
    config = AltBConfig(order_budget=Decimal(budget))
    margins = order_margins(config)
    notionals = order_notionals(config)
    assert isinstance(notionals, tuple) and len(margins) == len(notionals) == 8
    with localcontext(Context(prec=80, Emin=-999999, Emax=999999)):
        # 8 sumandos; unidad del último dígito al orden de magnitud del budget.
        tolerance = Decimal("8").scaleb(config.order_budget.adjusted() - MATH_PRECISION + 1)
        assert abs(sum(margins) - config.order_budget) <= tolerance
        assert abs(sum(notionals) - config.order_budget * 4) <= tolerance * 4
        baseline = order_margins(CONFIG)
        for margin, notional, base in zip(margins, notionals, baseline, strict=True):
            assert margin > 0
            assert notional > margin
            assert abs(margin - base * config.order_budget / Decimal("1200")) <= tolerance
            assert abs(margin / config.order_budget - base / Decimal("1200")) <= Decimal("8e-49")
    with localcontext(Context(prec=MATH_PRECISION)):
        assert all(
            notional == margin * 4 for margin, notional in zip(margins, notionals, strict=True)
        )


@pytest.mark.parametrize("level,expected", enumerate(DEVIATIONS, start=1))
def test_all_dca_deviations_match_exact_literals(level: int, expected: str) -> None:
    assert dca_cumulative_deviation(level, CONFIG) == Decimal(expected)


@pytest.mark.parametrize("level", [-1, 0, 8, 100])
def test_dca_level_out_of_range(level: int) -> None:
    with pytest.raises(ValueError):
        dca_cumulative_deviation(level, CONFIG)
    with pytest.raises(ValueError):
        dca_theoretical_price(Price(Decimal("100")), level, CONFIG)


@pytest.mark.parametrize("level", [True, False, 1.0, Decimal("1"), "1", None])
def test_dca_level_requires_real_int(level: object) -> None:
    with pytest.raises(TypeError):
        dca_cumulative_deviation(cast(int, level), CONFIG)
    with pytest.raises(TypeError):
        dca_theoretical_price(Price(Decimal("100")), cast(int, level), CONFIG)


@pytest.mark.parametrize("p0_literal", ["100", "0.0000123456789", "1234.56789"])
def test_every_dca_price_anchors_to_original_p0_and_decreases(p0_literal: str) -> None:
    p0 = Price(Decimal(p0_literal))
    original = p0.value
    prices = tuple(dca_theoretical_price(p0, level, CONFIG) for level in range(1, 8))
    deviations = tuple(dca_cumulative_deviation(level, CONFIG) for level in range(1, 8))
    with localcontext(Context(prec=80)):
        for price, deviation in zip(prices, map(Decimal, DEVIATIONS), strict=True):
            assert price.value == original * (1 - deviation)
        chained = original
        for deviation in map(Decimal, DEVIATIONS):
            chained *= 1 - deviation
        assert prices[-1].value != chained
    assert all(left.value > right.value for left, right in zip(prices, prices[1:], strict=False))
    assert all(left < right for left, right in zip(deviations, deviations[1:], strict=False))
    assert p0.value is original
    with pytest.raises(FrozenInstanceError):
        prices[-1].value = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "values,expected",
    [
        ((("123.456789", "2"),), "123.456789"),
        ((("100", "1"), ("80", "1")), "90"),
        ((("100", "1"), ("80", "3")), "85"),
        ((("100", "2"), ("80", "3"), ("70", "5")), "79"),
    ],
)
def test_weighted_average_uses_actual_quantities_and_preserves_fills(
    values: tuple[tuple[str, str], ...], expected: str
) -> None:
    fills = tuple(
        make_fill(price, quantity, index) for index, (price, quantity) in enumerate(values, 1)
    )
    original = tuple((fill.price.value, fill.quantity.value, fill.fill_id) for fill in fills)
    result = weighted_average_fill_price(fills)
    assert result == Price(Decimal(expected))
    assert (
        tuple((fill.price.value, fill.quantity.value, fill.fill_id) for fill in fills) == original
    )
    with pytest.raises(FrozenInstanceError):
        result.value = Decimal("1")  # type: ignore[misc]


def test_weighted_average_recurring_decimal_golden() -> None:
    fills = (make_fill("1", "1"), make_fill("2", "2", 2))
    assert weighted_average_fill_price(fills).value == Decimal(
        "1.6666666666666666666666666666666666666666666666667"
    )


def test_weighted_average_rejects_empty_tuple() -> None:
    with pytest.raises(ValueError):
        weighted_average_fill_price(())


@pytest.mark.parametrize(
    "fills", [[], [make_fill("100", "1")], None, ("fill",), (make_fill("1", "1"), object())]
)
def test_weighted_average_requires_tuple_of_actual_fills(fills: object) -> None:
    with pytest.raises(TypeError):
        weighted_average_fill_price(cast(tuple[Fill, ...], fills))


@pytest.mark.parametrize(
    "price,expected",
    [("100", "100.800"), ("85", "85.680"), ("0.0000123456789", "0.0000124444443312")],
)
def test_tp_is_exact_price_movement_without_leverage_or_other_adjustments(
    price: str, expected: str
) -> None:
    average = Price(Decimal(price))
    result = take_profit_theoretical_price(average, CONFIG)
    assert result.value == Decimal(expected)
    assert result.value == average.value * Decimal("1.008")
    assert result.value != average.value * Decimal("1.032")
    assert average.value == Decimal(price)
    with pytest.raises(FrozenInstanceError):
        result.value = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize("value", [Decimal("100"), 100, 100.0, "100", None])
def test_price_functions_require_price(value: object) -> None:
    with pytest.raises(TypeError):
        dca_theoretical_price(cast(Price, value), 1, CONFIG)
    with pytest.raises(TypeError):
        take_profit_theoretical_price(cast(Price, value), CONFIG)


def math_snapshot(config: AltBConfig, p0: Price, fills: tuple[Fill, ...]) -> tuple[object, ...]:
    return (
        order_weights(config),
        order_margins(config),
        order_notionals(config),
        tuple(dca_cumulative_deviation(level, config) for level in range(1, 8)),
        tuple(dca_theoretical_price(p0, level, config) for level in range(1, 8)),
        weighted_average_fill_price(fills),
        take_profit_theoretical_price(p0, config),
    )


@pytest.mark.parametrize("precision", [2, 6, 28, 80])
def test_all_math_is_independent_of_global_decimal_context_and_does_not_mutate_it(
    precision: int,
) -> None:
    assert MATH_PRECISION == 50
    config = AltBConfig(order_budget=Decimal("1234.567890123456789012345678901234567890123456789"))
    p0 = Price(Decimal("0.00001234567890123456789012345678901234567890123456789"))
    fills = (make_fill("1", "1"), make_fill("2", "2", 2))
    baseline = math_snapshot(config, p0, fills)
    with localcontext() as ambient:
        ambient.prec = precision
        ambient.rounding = ROUND_DOWN
        ambient.Emin = -9
        ambient.Emax = 9
        ambient.clamp = 1
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        ambient.clear_flags()
        before = ambient.copy()
        assert math_snapshot(config, p0, fills) == baseline
        assert ambient.prec == before.prec
        assert ambient.rounding == before.rounding
        assert ambient.Emin == before.Emin and ambient.Emax == before.Emax
        assert ambient.clamp == before.clamp
        assert ambient.traps == before.traps and ambient.flags == before.flags
