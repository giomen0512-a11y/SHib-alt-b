"""Intención, snapshot de orden y fill son conceptos separados."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from trading_engine.models._validation import (
    finite_decimal,
    nonblank,
    require_type,
    utc_timestamp,
)
from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.ids import FillId, IntentId, OrderId, RoundId
from trading_engine.models.values import Price, Quantity, Symbol


@dataclass(frozen=True)
class OrderIntent:
    intent_id: IntentId
    round_id: RoundId
    symbol: Symbol
    side: Side
    order_type: OrderType
    quantity: Quantity
    created_at: datetime
    limit_price: Price | None = None
    reduce_only: bool = False

    def __post_init__(self) -> None:
        require_type(self.intent_id, UUID, "intent_id")
        require_type(self.round_id, UUID, "round_id")
        require_type(self.symbol, Symbol, "symbol")
        require_type(self.side, Side, "side")
        require_type(self.order_type, OrderType, "order_type")
        require_type(self.quantity, Quantity, "quantity")
        require_type(self.reduce_only, bool, "reduce_only")
        utc_timestamp(self.created_at, "created_at")
        if self.order_type is OrderType.LIMIT:
            require_type(self.limit_price, Price, "limit_price de una LIMIT")
        elif self.limit_price is not None:
            raise ValueError("MARKET no admite limit_price")


@dataclass(frozen=True)
class Order:
    order_id: OrderId
    intent: OrderIntent
    status: OrderStatus
    filled_quantity: Decimal
    created_at: datetime
    updated_at: datetime
    adapter_order_id: str | None = None

    def __post_init__(self) -> None:
        require_type(self.order_id, UUID, "order_id")
        require_type(self.intent, OrderIntent, "intent")
        require_type(self.status, OrderStatus, "status")
        finite_decimal(self.filled_quantity, "filled_quantity", nonnegative=True)
        requested = self.requested_quantity.value
        if self.filled_quantity > requested:
            raise ValueError("filled_quantity no puede superar requested_quantity")
        if self.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.REJECTED):
            if self.filled_quantity != 0:
                raise ValueError("Este status requiere filled_quantity cero")
        elif self.status is OrderStatus.PARTIALLY_FILLED:
            if not 0 < self.filled_quantity < requested:
                raise ValueError("PARTIALLY_FILLED requiere un fill parcial")
        elif self.status is OrderStatus.FILLED:
            if self.filled_quantity != requested:
                raise ValueError("FILLED requiere la cantidad completa")
        elif self.status is OrderStatus.CANCELED and self.filled_quantity == requested:
            raise ValueError("Una orden completa debe ser FILLED, no CANCELED")
        utc_timestamp(self.created_at, "created_at")
        utc_timestamp(self.updated_at, "updated_at")
        if self.created_at < self.intent.created_at or self.updated_at < self.created_at:
            raise ValueError("Timestamps de orden incoherentes con la intención")
        if self.adapter_order_id is not None:
            nonblank(self.adapter_order_id, "adapter_order_id")

    @property
    def intent_id(self) -> IntentId:
        return self.intent.intent_id

    @property
    def round_id(self) -> RoundId:
        return self.intent.round_id

    @property
    def symbol(self) -> Symbol:
        return self.intent.symbol

    @property
    def requested_quantity(self) -> Quantity:
        return self.intent.quantity


@dataclass(frozen=True)
class Fill:
    fill_id: FillId
    order_id: OrderId
    timestamp: datetime
    price: Price
    quantity: Quantity

    def __post_init__(self) -> None:
        require_type(self.fill_id, UUID, "fill_id")
        require_type(self.order_id, UUID, "order_id")
        utc_timestamp(self.timestamp, "timestamp")
        require_type(self.price, Price, "price")
        require_type(self.quantity, Quantity, "quantity")
