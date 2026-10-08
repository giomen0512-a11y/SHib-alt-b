from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_engine.models.market import Quote
from trading_engine.models.values import Price


@pytest.mark.parametrize("ask", ["2", "2.1"])
def test_quote_accepts_positive_equal_or_ordered_prices(quote: Quote, ask: str) -> None:
    result = replace(quote, ask=Price(Decimal(ask)))
    assert result.bid.value <= result.ask.value
    assert result.timestamp is quote.timestamp


def test_quote_rejects_crossed_prices(quote: Quote) -> None:
    with pytest.raises(ValueError, match="bid"):
        replace(quote, ask=Price(Decimal("1")))


@pytest.mark.parametrize("field", ["bid", "ask"])
@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_quote_cannot_contain_nonpositive_or_nonfinite_prices(
    quote: Quote, field: str, value: str
) -> None:
    with pytest.raises(ValueError):
        replace(quote, **{field: Price(Decimal(value))})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"symbol": "SHIBUSDC"}, TypeError),
        ({"timestamp": datetime(2026, 1, 1)}, ValueError),
        ({"timestamp": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))}, ValueError),
        ({"timestamp": None}, TypeError),
        ({"timestamp": "2026-01-01"}, TypeError),
        ({"bid": Decimal("2")}, TypeError),
        ({"ask": Decimal("2.1")}, TypeError),
        ({"bid": 2.0}, TypeError),
        ({"ask": "2.1"}, TypeError),
    ],
)
def test_quote_rejects_invalid_fields(
    quote: Quote, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(quote, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", [item.name for item in fields(Quote)])
def test_quote_is_frozen(quote: Quote, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(quote, field, None)
