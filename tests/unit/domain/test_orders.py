from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.ids import FillId
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.values import Price, Quantity


@pytest.mark.parametrize("reduce_only", [True, False])
def test_intent_accepts_immutable_reduce_only(intent: OrderIntent, reduce_only: bool) -> None:
    result = replace(intent, reduce_only=reduce_only)
    assert result.reduce_only is reduce_only
    with pytest.raises(FrozenInstanceError):
        result.reduce_only = not reduce_only  # type: ignore[misc]


def test_intent_defaults_to_non_reduce_only(intent: OrderIntent) -> None:
    assert intent.reduce_only is False


@pytest.mark.parametrize("value", [0, 1, -1, "true", "false", "", None, Decimal("1"), 0.0])
def test_intent_rejects_non_boolean_reduce_only(intent: OrderIntent, value: object) -> None:
    with pytest.raises(TypeError, match="reduce_only debe ser bool"):
        replace(intent, reduce_only=value)  # type: ignore[arg-type]


def test_intent_is_quantity_based_and_immutable(intent: OrderIntent) -> None:
    assert intent.limit_price is None
    limit = replace(intent, order_type=OrderType.LIMIT, limit_price=Price(Decimal("2")))
    assert limit.quantity == intent.quantity
    assert replace(intent, side=Side.SELL).side is Side.SELL
    with pytest.raises(FrozenInstanceError):
        intent.quantity = Quantity(Decimal("1"))  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"order_type": OrderType.LIMIT}, TypeError),
        ({"limit_price": Price(Decimal("1"))}, ValueError),
        ({"order_type": OrderType.LIMIT, "limit_price": 1.0}, TypeError),
        ({"quantity": 1.0}, TypeError),
        ({"quantity": Decimal("0")}, TypeError),
        ({"intent_id": "id"}, TypeError),
        ({"round_id": "id"}, TypeError),
        ({"side": "BUY"}, TypeError),
        ({"order_type": "MARKET"}, TypeError),
        ({"symbol": "SHIBUSDC"}, TypeError),
        ({"timestamp": datetime(2026, 1, 1)}, ValueError),
    ],
)
def test_intent_rejects_ambiguous_or_invalid_fields(
    intent: OrderIntent, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(intent, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "status, filled",
    [
        (OrderStatus.PENDING, "0"),
        (OrderStatus.OPEN, "0"),
        (OrderStatus.PARTIALLY_FILLED, "1"),
        (OrderStatus.FILLED, "10"),
        (OrderStatus.CANCELED, "0"),
        (OrderStatus.CANCELED, "5"),
        (OrderStatus.REJECTED, "0"),
    ],
)
def test_order_represents_partial_and_terminal_snapshots(
    order: Order, status: OrderStatus, filled: str
) -> None:
    result = replace(order, status=status, filled_quantity=Decimal(filled), adapter_order_id="abc")
    assert result.filled_quantity == Decimal(filled)
    assert result.intent_id == result.intent.intent_id
    assert result.round_id == result.intent.round_id
    assert result.symbol == result.intent.symbol
    assert result.requested_quantity == result.intent.quantity


@pytest.mark.parametrize(
    "status, filled",
    [
        (OrderStatus.OPEN, "1"),
        (OrderStatus.PENDING, "1"),
        (OrderStatus.REJECTED, "1"),
        (OrderStatus.PARTIALLY_FILLED, "0"),
        (OrderStatus.PARTIALLY_FILLED, "10"),
        (OrderStatus.FILLED, "9"),
        (OrderStatus.CANCELED, "10"),
        (OrderStatus.CANCELED, "11"),
        (OrderStatus.OPEN, "-1"),
    ],
)
def test_order_rejects_inconsistent_status_and_fill(
    order: Order, status: OrderStatus, filled: str
) -> None:
    with pytest.raises(ValueError):
        replace(order, status=status, filled_quantity=Decimal(filled))


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), 0.1])
def test_order_rejects_invalid_filled_quantity(order: Order, value: object) -> None:
    error = ValueError if isinstance(value, Decimal) else TypeError
    with pytest.raises(error):
        replace(order, filled_quantity=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"order_id": "id"}, TypeError),
        ({"intent": None}, TypeError),
        ({"status": "OPEN"}, TypeError),
        ({"adapter_order_id": " "}, ValueError),
        ({"created_at": datetime(2026, 1, 1)}, ValueError),
        ({"updated_at": datetime(2026, 1, 1)}, ValueError),
    ],
)
def test_order_rejects_invalid_fields(
    order: Order, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(order, **changes)  # type: ignore[arg-type]


def test_order_requires_causal_timestamps(order: Order) -> None:
    with pytest.raises(ValueError):
        replace(order, created_at=order.intent.timestamp - timedelta(seconds=1))
    with pytest.raises(ValueError):
        replace(order, updated_at=order.created_at - timedelta(seconds=1))
    assert replace(order, updated_at=order.created_at + timedelta(seconds=1))


def test_fill_is_independent_of_order_snapshot(order: Order) -> None:
    fill = Fill(
        FillId(UUID(int=4)),
        order.order_id,
        order.created_at,
        Price(Decimal("2")),
        Quantity(Decimal("1")),
    )
    assert fill.order_id == order.order_id
    assert order.filled_quantity == 0
    with pytest.raises(FrozenInstanceError):
        fill.quantity = Quantity(Decimal("2"))  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"price": 1.0}, TypeError),
        ({"price": Decimal("0")}, TypeError),
        ({"quantity": 1.0}, TypeError),
        ({"quantity": Decimal("-1")}, TypeError),
        ({"fill_id": "id"}, TypeError),
        ({"order_id": "id"}, TypeError),
        ({"timestamp": datetime(2026, 1, 1)}, ValueError),
    ],
)
def test_fill_rejects_invalid_fields(
    order: Order, changes: dict[str, object], error: type[Exception]
) -> None:
    fill = Fill(
        FillId(UUID(int=4)),
        order.order_id,
        order.created_at,
        Price(Decimal("2")),
        Quantity(Decimal("1")),
    )
    with pytest.raises(error):
        replace(fill, **changes)  # type: ignore[arg-type]
