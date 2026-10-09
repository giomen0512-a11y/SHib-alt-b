"""Mercado y hechos de ejecución completamente sintéticos; bases pequeñas."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from trading_engine.models.enums import OrderStatus
from trading_engine.models.ids import FillId, OrderId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.values import Price, Quantity, Symbol
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinatorState
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source
from trading_engine.replay.models import ReplayRequest
from trading_engine.replay.ports import FillEvidence, FixtureFacts, FixtureQuotes, ReplayFact
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.engine import AltBEngine
from trading_engine.strategy.alt_b.events.state import (
    AltBStrategyState,
    PendingEntry,
    entry_identity,
)
from trading_engine.strategy.alt_b.state_market import AltBMarketState

T = datetime(2026, 1, 2, 12, tzinfo=UTC)
START = T - timedelta(minutes=15)
DUE = T + timedelta(seconds=120)
END = T + timedelta(minutes=5)
SYMBOL = Symbol("1000SHIBUSDC")


@pytest.fixture(scope="module")
def candles() -> tuple[Candle, ...]:
    first = START - timedelta(minutes=1560)
    return tuple(
        Candle(
            SYMBOL,
            first + timedelta(minutes=i),
            first + timedelta(minutes=i + 1),
            Price(Decimal("100")),
            Price(Decimal("103")),
            Price(Decimal("99")),
            Price(Decimal("100") if i % 2 == 0 else Decimal("101")),
            Decimal("1"),
            Decimal("0.5"),
        )
        for i in range(1580)
    )


def write_db(path: Path, candles: tuple[Candle, ...]) -> Path:
    with FuturesSQLite(path) as store:
        store.import_candles(
            candles,
            symbol=SYMBOL,
            source=Source.MONTHLY,
            file="synthetic-fixture",
            period="fixture",
            as_of=max(c.close_time for c in candles),
        )
    return path


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory, candles: tuple[Candle, ...]) -> Path:
    return write_db(tmp_path_factory.mktemp("replay") / "fixture.sqlite3", candles)


@pytest.fixture
def replay_request() -> ReplayRequest:
    return ReplayRequest(START, END, allow_entries=True)


@pytest.fixture
def quotes() -> FixtureQuotes:
    return FixtureQuotes((Quote(SYMBOL, DUE, Price(Decimal("99")), Price(Decimal("100"))),))


def order_for(
    intent: OrderIntent,
    number: int,
    at: datetime,
    status: OrderStatus = OrderStatus.OPEN,
    qty: Decimal = Decimal("0"),
) -> Order:
    return Order(OrderId(UUID(int=number)), intent, status, qty, intent.created_at, at)


def fill_for(order: Order, number: int, price: str = "100") -> Fill:
    return Fill(
        FillId(UUID(int=number)),
        order.order_id,
        order.updated_at,
        Price(Decimal(price)),
        Quantity(order.filled_quantity),
    )


@pytest.fixture
def base_fact(candles: tuple[Candle, ...], quotes: FixtureQuotes) -> ReplayFact:
    history = tuple(c for c in candles if T - timedelta(minutes=1560) <= c.open_time < T)
    signal, action = entry_identity(AltBConfig(), T)
    state = CoordinatorState(
        (action,),
        AltBStrategyState(
            market=AltBMarketState(history),
            allow_entries=True,
            pending_entry=PendingEntry(signal, action),
            last_event_at=T,
        ),
    )
    coordinator = StrategyCoordinator(AltBEngine())
    base = coordinator.on_scheduled_action(state, action, quotes.quotes[0]).order_intents[0]
    order = order_for(
        base, 100, DUE + timedelta(seconds=1), OrderStatus.FILLED, base.quantity.value
    )
    return ReplayFact(order.updated_at, FillEvidence(fill_for(order, 1000), order), 0)


@pytest.fixture
def facts(base_fact: ReplayFact) -> FixtureFacts:
    return FixtureFacts(
        (base_fact, replace(base_fact, received_at=DUE + timedelta(seconds=2), sequence=1))
    )
