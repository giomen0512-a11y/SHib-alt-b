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
from trading_engine.strategy.alt_b.events.execution_state import (
    canonical_fills,
    filled_quantity,
    merge_fill,
    merge_order,
    validate_fill,
    validate_order,
)
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
        object.__setattr__(self, "fills", canonical_fills(self.fills))
        if self.reconciliation_reason is not None:
            nonblank(self.reconciliation_reason, "reconciliation_reason")
            if self.reconciliation_reason != "base_order_conflict":
                raise ValueError("Razón de reconciliación no reconocida")

    def validate_order(self, order: Order) -> None:
        validate_order(self.intent, self.order, order)

    def validate_fill(self, fill: Fill) -> None:
        validate_fill(self.intent, self.order, fill)

    @property
    def executed_quantity(self) -> Decimal:
        return filled_quantity(self.fills)

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
    order, conflict = merge_order(base.order, order)
    return replace(
        base,
        order=order,
        reconciliation_reason="base_order_conflict" if conflict else base.reconciliation_reason,
    )


def observe_fill(base: BaseState, fill: Fill) -> BaseState:
    base.validate_fill(fill)
    return replace(base, fills=merge_fill(base.fills, fill))
