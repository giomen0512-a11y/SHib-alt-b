"""Reserva de Base y evidencia de ejecución, puras y sin adaptadores."""

from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from typing import Final

from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.identity import make_intent_id
from trading_engine.models.market import Quote
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.values import Price, Quantity
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.math import (
    _context,
    order_notionals,
    weighted_average_fill_price,
)

BASE_PURPOSE: Final[str] = "ALT_B_BASE"
TERMINAL_STATUSES: Final[tuple[OrderStatus, ...]] = (
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
)


def base_intent(action: ScheduledAction, quote: Quote, config: AltBConfig) -> OrderIntent:
    """Ask explícito como referencia BUY; cantidad teórica sin cuantizar."""
    notional = order_notionals(config)[0]  # La fórmula canónica ya aplica 4x.
    with localcontext(_context()):
        quantity = Quantity(notional / quote.ask.value)
    return OrderIntent(
        make_intent_id(action.round_id, BASE_PURPOSE, quote.timestamp, str(action.action_id)),
        action.round_id,
        action.symbol,
        Side.BUY,
        OrderType.MARKET,
        quantity,
        quote.timestamp,
        reduce_only=False,
    )


@dataclass(frozen=True)
class BaseState:
    action: ScheduledAction
    reference_quote: Quote
    intent: OrderIntent
    order: Order | None = None
    fills: tuple[Fill, ...] = ()
    reconciliation_reason: str | None = None

    def __post_init__(self) -> None:
        require_type(self.action, ScheduledAction, "action")
        require_type(self.reference_quote, Quote, "reference_quote")
        require_type(self.intent, OrderIntent, "intent")
        quote = self.reference_quote
        intent = self.intent
        if (
            quote.symbol != self.action.symbol
            or quote.timestamp < self.action.not_before
            or intent.symbol != self.action.symbol
            or intent.round_id != self.action.round_id
            or intent.created_at != quote.timestamp
            or intent.side is not Side.BUY
            or intent.order_type is not OrderType.MARKET
            or intent.reduce_only
            or intent.intent_id
            != make_intent_id(
                self.action.round_id, BASE_PURPOSE, quote.timestamp, str(self.action.action_id)
            )
        ):
            raise ValueError("Identidad o referencia de Base incompatible")
        if self.order is not None:
            self.validate_order(self.order)
        require_type(self.fills, tuple, "fills")
        for fill in self.fills:
            self.validate_fill(fill)
        if len({f.fill_id for f in self.fills}) != len(self.fills):
            raise ValueError("FillId duplicado en snapshot")
        object.__setattr__(self, "fills", tuple(sorted(self.fills, key=lambda f: f.fill_id.int)))
        if self.reconciliation_reason is not None:
            nonblank(self.reconciliation_reason, "reconciliation_reason")
            if self.reconciliation_reason != "base_order_conflict":
                raise ValueError("Razón de reconciliación no reconocida")

    def validate_order(self, order: Order) -> None:
        require_type(order, Order, "order")
        if order.intent != self.intent:
            raise ValueError("Order no corresponde a intención, ronda o símbolo de Base")
        if self.order is not None:
            if order.order_id != self.order.order_id or order.created_at != self.order.created_at:
                raise ValueError("Una Base no puede vincularse a otra Order")
            if (
                order.adapter_order_id is not None
                and self.order.adapter_order_id is not None
                and order.adapter_order_id != self.order.adapter_order_id
            ):
                raise ValueError("Identidad externa de Order conflictiva")

    def validate_fill(self, fill: Fill) -> None:
        require_type(fill, Fill, "fill")
        if self.order is None or fill.order_id != self.order.order_id:
            raise ValueError("Fill no corresponde a Order de Base")
        if fill.timestamp < self.intent.created_at:
            raise ValueError("Fill anterior a intención de Base")

    @property
    def executed_quantity(self) -> Decimal:
        with localcontext(_context()):
            return sum((f.quantity.value for f in self.fills), Decimal("0"))

    @property
    def average_fill_price(self) -> Price | None:
        return weighted_average_fill_price(self.fills) if self.fills else None

    @property
    def reconciliation_required(self) -> bool:
        return self.block_reason in {
            "base_order_conflict",
            "base_quantity_mismatch",
            "base_overfill",
            "base_shortfill_reconciliation",
        }

    @property
    def order_pending(self) -> bool:
        return (
            self.order is None
            or self.order.status not in TERMINAL_STATUSES
            or self.reconciliation_required
        )

    @property
    def p0(self) -> Price | None:
        # Un promedio parcial es evidencia; P0 definitivo requiere Base resuelta.
        if self.order is None or self.order_pending:
            return None
        return self.average_fill_price

    @property
    def block_reason(self) -> str:
        if self.reconciliation_reason is not None:
            return self.reconciliation_reason
        qty = self.executed_quantity
        if qty > self.intent.quantity.value:
            return "base_overfill"
        if self.order is not None:
            if qty != self.order.filled_quantity:
                return "base_quantity_mismatch"
            if self.order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                if qty > 0:
                    return "base_shortfill_reconciliation"
                return (
                    "base_order_canceled"
                    if self.order.status is OrderStatus.CANCELED
                    else "base_order_rejected"
                )
            if self.order.status is OrderStatus.FILLED:
                return "base_confirmed"
        return "base_order_pending"


def observe_order(base: BaseState, order: Order) -> BaseState:
    """Conservar el snapshot más reciente sin hacer rollback por entrega tardía."""
    base.validate_order(order)
    previous = base.order
    if previous is None:
        return replace(base, order=order)
    # La omisión en un snapshot no borra una identidad externa ya conocida.
    adapter_id = previous.adapter_order_id or order.adapter_order_id
    previous = replace(previous, adapter_order_id=adapter_id)
    order = replace(order, adapter_order_id=adapter_id)
    if order == previous:
        return replace(base, order=previous)
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
            and newer.status in (OrderStatus.PENDING, OrderStatus.OPEN)
        )
    )
    return replace(
        base,
        order=newer,
        reconciliation_reason="base_order_conflict" if conflict else base.reconciliation_reason,
    )


def observe_fill(base: BaseState, fill: Fill) -> BaseState:
    base.validate_fill(fill)
    for previous in base.fills:
        if previous.fill_id == fill.fill_id:
            if previous != fill:
                raise ValueError("FillId repetido con contenido conflictivo")
            return base
    # Incluso un overfill conserva la evidencia y bloquea, no la descarta.
    return replace(base, fills=base.fills + (fill,))
