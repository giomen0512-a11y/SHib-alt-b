from decimal import Decimal, InvalidOperation

import pytest

from trading_engine.utils.decimal import to_decimal


@pytest.mark.parametrize("value", ["0.00000123", 42, Decimal("123.4500")])
def test_exact_decimal(value: str | int | Decimal) -> None:
    result = to_decimal(value)
    assert isinstance(result, Decimal)
    assert result == Decimal(value)


def test_decimal_arithmetic_is_exact() -> None:
    assert to_decimal("0.1") + to_decimal("0.2") == Decimal("0.3")


@pytest.mark.parametrize("value", [0.1, True])
def test_rejects_float_and_bool(value: float | bool) -> None:
    with pytest.raises(TypeError):
        to_decimal(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_rejects_non_finite(value: str) -> None:
    with pytest.raises(ValueError):
        to_decimal(value)


def test_invalid_input_is_not_hidden() -> None:
    with pytest.raises(InvalidOperation):
        to_decimal("invalid")
