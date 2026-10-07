from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import cast

import pytest

from trading_engine.models.enums import OrderStatus, OrderType, PositionSide, RoundStatus, Side
from trading_engine.models.values import Money, Price, Quantity, Symbol


@pytest.mark.parametrize("model", [Price, Quantity])
@pytest.mark.parametrize(
    "value",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        0.1,
        1,
        True,
        "1",
        None,
    ],
)
def test_positive_values_reject_invalid(model: type[Price] | type[Quantity], value: object) -> None:
    error = ValueError if isinstance(value, Decimal) else TypeError
    with pytest.raises(error):
        model(cast(Decimal, value))


@pytest.mark.parametrize("model", [Price, Quantity])
def test_positive_values_preserve_exact_decimal(model: type[Price] | type[Quantity]) -> None:
    value = Decimal("0.0000012300")
    result = model(value)
    assert result.value is value
    with pytest.raises(FrozenInstanceError):
        result.value = Decimal("2")  # type: ignore[misc]


@pytest.mark.parametrize("value", ["", " ", "SHIB USDC", "SHIB\tUSDC", "SHIB\nUSDC", " SHIB"])
def test_symbol_rejects_blank_and_whitespace(value: str) -> None:
    with pytest.raises(ValueError):
        Symbol(value)


def test_symbol_normalizes_without_catalog_knowledge() -> None:
    assert Symbol("shibusdc") == Symbol("SHIBUSDC")
    assert Symbol("vendor-pair").value == "VENDOR-PAIR"
    with pytest.raises(TypeError):
        Symbol(cast(str, 123))


@pytest.mark.parametrize("amount", ["0", "-1.2500", "100.0001"])
def test_money_accepts_zero_and_signed_amounts(amount: str) -> None:
    value = Decimal(amount)
    money = Money(value, "usdc")
    assert money.amount is value
    assert money.currency == "USDC"
    assert money != Money(value, "USD")


@pytest.mark.parametrize("value", [0.1, True, 1, "1", Decimal("NaN"), Decimal("Infinity")])
def test_money_rejects_inexact_or_nonfinite(value: object) -> None:
    error = ValueError if isinstance(value, Decimal) else TypeError
    with pytest.raises(error):
        Money(cast(Decimal, value), "USDC")


@pytest.mark.parametrize("currency", ["", " ", "US DC", "USDC\n"])
def test_money_rejects_invalid_currency(currency: str) -> None:
    with pytest.raises(ValueError):
        Money(Decimal("0"), currency)


def test_domain_enum_vocabulary() -> None:
    assert {value.value for value in Side} == {"BUY", "SELL"}
    assert {value.value for value in PositionSide} == {"LONG", "SHORT"}
    assert {value.value for value in OrderType} == {"MARKET", "LIMIT"}
    assert {value.value for value in OrderStatus} == {
        "PENDING",
        "OPEN",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
    }
    assert {value.value for value in RoundStatus} == {
        "PENDING_ENTRY",
        "OPEN",
        "CLOSING",
        "CLOSED",
        "FAILED",
    }
