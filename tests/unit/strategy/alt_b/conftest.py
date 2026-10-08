from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol


@pytest.fixture(scope="module")
def checkpoint() -> datetime:
    return datetime(2026, 1, 2, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def history(checkpoint: datetime) -> tuple[Candle, ...]:
    start = checkpoint - timedelta(hours=26)
    return tuple(
        Candle(
            Symbol("1000SHIBUSDC"),
            start + timedelta(minutes=i),
            start + timedelta(minutes=i + 1),
            Price(Decimal("100")),
            Price(Decimal("103")),
            Price(Decimal("99")),
            Price(Decimal("100") if i % 2 == 0 else Decimal("101")),
            Decimal("1"),
            Decimal("0.5"),
        )
        for i in range(1560)
    )
