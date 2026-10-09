"""Hechos sintéticos offline; no cotizaciones ni ejecuciones reales."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.replay.models import EntryControlEvent, ReplayState
from trading_engine.replay.runner import ReplayRunner

SYMBOL = Symbol("1000SHIBUSDC")
T = datetime(2026, 1, 2, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Replay 3C-1 no puede consultar red ni ejecutar órdenes")

    monkeypatch.setattr("trading_engine.binance.public_data.open_official", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)


@pytest.fixture(scope="module")
def history() -> tuple[Candle, ...]:
    start = T - timedelta(hours=26)
    return tuple(
        Candle(
            SYMBOL,
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


@pytest.fixture(scope="module")
def signaled(history: tuple[Candle, ...]) -> ReplayState:
    return ReplayRunner().run((EntryControlEvent(history[0].open_time, True), *history))
