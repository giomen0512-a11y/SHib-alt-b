"""Evidencia común de órdenes: sin inferir ejecuciones ni consultar proveedores."""

from dataclasses import dataclass, replace
from decimal import Decimal, localcontext

from trading_engine.models._validation import require_type
from trading_engine.models.enums import OrderStatus
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.strategy.alt_b.math import _context

TERMINAL_STATUSES = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)


def validate_order(intent: OrderIntent, previous: Order | None, order: Order) -> None:
    require_type(order, Order, "order")
    if order.intent != intent:
        raise ValueError("Order no corresponde a intención, ronda, símbolo, lado o propósito")
    if previous is not None:
        if order.order_id != previous.order_id or order.created_at != previous.created_at:
            raise ValueError("Una intención no puede vincularse a otra Order")
        if (
            previous.adapter_order_id is not None
            and order.adapter_order_id is not None
            and previous.adapter_order_id != order.adapter_order_id
        ):
            raise ValueError("Identidad externa de Order conflictiva")


def merge_order(previous: Order | None, order: Order) -> tuple[Order, bool]:
    if previous is None:
        return order, False
    adapter_id = previous.adapter_order_id or order.adapter_order_id
    previous = replace(previous, adapter_order_id=adapter_id)
    order = replace(order, adapter_order_id=adapter_id)
    if order == previous:
        return previous, False
    if order.updated_at == previous.updated_at:
        raise ValueError("Snapshots de Order conflictivos en el mismo instante")
    older, newer = (
        (order, previous) if order.updated_at < previous.updated_at else (previous, order)
    )
    conflict = (
        newer.filled_quantity < older.filled_quantity
        or (older.status in TERMINAL_STATUSES and newer.status != older.status)
        or (older.status in TERMINAL_STATUSES and newer.filled_quantity != older.filled_quantity)
        or (older.status is OrderStatus.OPEN and newer.status is OrderStatus.PENDING)
        or (
            older.status is OrderStatus.PARTIALLY_FILLED
            and newer.status in (OrderStatus.OPEN, OrderStatus.PENDING)
        )
    )
    return newer, conflict


def validate_fill(intent: OrderIntent, order: Order | None, fill: Fill) -> None:
    require_type(fill, Fill, "fill")
    if order is None or fill.order_id != order.order_id:
        raise ValueError("Fill no corresponde a Order")
    if fill.timestamp < intent.created_at:
        raise ValueError("Fill anterior a intención")


def canonical_fills(fills: tuple[Fill, ...]) -> tuple[Fill, ...]:
    require_type(fills, tuple, "fills")
    for fill in fills:
        require_type(fill, Fill, "fill")
    if len({f.fill_id for f in fills}) != len(fills):
        raise ValueError("FillId duplicado en snapshot")
    return tuple(sorted(fills, key=lambda f: f.fill_id.int))


def merge_fill(fills: tuple[Fill, ...], fill: Fill) -> tuple[Fill, ...]:
    for previous in fills:
        if previous.fill_id == fill.fill_id:
            if previous != fill:
                raise ValueError("FillId repetido con contenido conflictivo")
            return fills
    return canonical_fills(fills + (fill,))


def filled_quantity(fills: tuple[Fill, ...]) -> Decimal:
    with localcontext(_context()):
        return sum((f.quantity.value for f in fills), Decimal("0"))


@dataclass(frozen=True)
class OrderLeg:
    intent: OrderIntent
    order: Order | None = None
    fills: tuple[Fill, ...] = ()
    order_conflict: bool = False

    def __post_init__(self) -> None:
        require_type(self.intent, OrderIntent, "intent")
        require_type(self.order_conflict, bool, "order_conflict")
        if self.order is not None:
            validate_order(self.intent, self.order, self.order)
        object.__setattr__(self, "fills", canonical_fills(self.fills))
        for fill in self.fills:
            validate_fill(self.intent, self.order, fill)

    @property
    def executed_quantity(self) -> Decimal:
        return filled_quantity(self.fills)

    @property
    def reconciliation_required(self) -> bool:
        return (
            self.order_conflict
            or self.executed_quantity > self.intent.quantity.value
            or (self.order is not None and self.order.filled_quantity != self.executed_quantity)
        )

    @property
    def settled(self) -> bool:
        return (
            self.order is not None
            and self.order.status in TERMINAL_STATUSES
            and not self.reconciliation_required
        )

    def observe(self, order: Order, fill: Fill | None = None) -> "OrderLeg":
        validate_order(self.intent, self.order, order)
        latest, conflict = merge_order(self.order, order)
        fills = self.fills
        if fill is not None:
            validate_fill(self.intent, latest, fill)
            fills = merge_fill(fills, fill)
        return replace(
            self, order=latest, fills=fills, order_conflict=self.order_conflict or conflict
        )
