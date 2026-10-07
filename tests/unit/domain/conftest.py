from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_engine.models.account import BalanceSnapshot, Position
from trading_engine.models.enums import OrderStatus, OrderType, PositionSide, RoundStatus, Side
from trading_engine.models.ids import IntentId, OrderId, RoundId
from trading_engine.models.market import Candle
from trading_engine.models.orders import Order, OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.values import Money, Price, Quantity, Symbol


@pytest.fixture
def timestamp() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def candle(timestamp: datetime) -> Candle:
    return Candle(
        Symbol("SHIBUSDC"),
        timestamp,
        timestamp + timedelta(minutes=5),
        Price(Decimal("2")),
        Price(Decimal("3")),
        Price(Decimal("1")),
        Price(Decimal("2.5")),
        Decimal("0"),
    )


@pytest.fixture
def intent(timestamp: datetime) -> OrderIntent:
    return OrderIntent(
        IntentId(UUID(int=1)),
        RoundId(UUID(int=2)),
        Symbol("SHIBUSDC"),
        Side.BUY,
        OrderType.MARKET,
        Quantity(Decimal("10")),
        timestamp,
    )


@pytest.fixture
def order(intent: OrderIntent) -> Order:
    return Order(
        OrderId(UUID(int=3)),
        intent,
        OrderStatus.OPEN,
        Decimal("0"),
        intent.timestamp,
        intent.timestamp,
    )


@pytest.fixture
def position(timestamp: datetime) -> Position:
    return Position(
        Symbol("SHIBUSDC"),
        PositionSide.LONG,
        Quantity(Decimal("10")),
        Price(Decimal("2")),
        timestamp,
    )


@pytest.fixture
def round_state(timestamp: datetime) -> Round:
    return Round(RoundId(UUID(int=2)), Symbol("SHIBUSDC"), RoundStatus.PENDING_ENTRY, timestamp)


@pytest.fixture
def balance(timestamp: datetime) -> BalanceSnapshot:
    return BalanceSnapshot(Money(Decimal("100"), "USDC"), Money(Decimal("80"), "USDC"), timestamp)
