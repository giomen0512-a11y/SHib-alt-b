"""Retenciones por remanente confirmado; solicitar cancelación no libera fondos."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.orders import Order, OrderCancelRequest
from trading_engine.models.values import Symbol
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance.evidence import Origin
from trading_engine.replay.financial_performance.models import Metric
from trading_engine.replay.isolated_margin_risk.models import RiskContext
from trading_engine.replay.reserve_transfer_simulation.models import (
    OrderMarginReservation,
    ReserveState,
)
from trading_engine.replay.reserve_transfer_simulation.policy import ReservationPolicy

ZERO = Decimal(0)
TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}


def money(value: Decimal | None, reason: str = "") -> Metric:
    return Metric(
        value,
        "MODELED" if value is not None else "NOT_AVAILABLE",
        (reason,) if reason else (),
        (Origin.MODELED,),
    )


def receive_order(state: ReserveState, order: Order, at: datetime) -> ReserveState:
    if order.updated_at > at or order.symbol != Symbol("1000SHIBUSDC"):
        raise ValueError("Orden futura o símbolo incompatible")
    previous = next((o for o in state.orders if o.order_id == order.order_id), None)
    if previous is not None:
        if previous == order:
            return state
        if (
            previous.intent != order.intent
            or order.updated_at < previous.updated_at
            or order.filled_quantity < previous.filled_quantity
            or previous.status in TERMINAL
        ):
            raise ValueError("Estado de orden contradictorio/tardío después de terminal")
        if order.updated_at == previous.updated_at and previous.status is not OrderStatus.PENDING:
            # Varios fills confirmados del mismo instante tienen cantidad acumulada creciente.
            if order.filled_quantity <= previous.filled_quantity:
                raise ValueError("Actualizaciones simultáneas de orden ambiguas")
    elif any(o.intent_id == order.intent_id for o in state.orders):
        raise ValueError("Una intención vinculada a dos OrderId")
    return replace(
        state, orders=tuple(o for o in state.orders if o.order_id != order.order_id) + (order,)
    )


def receive_cancel(state: ReserveState, request: OrderCancelRequest, at: datetime) -> ReserveState:
    if request.created_at > at:
        raise ValueError("Cancelación futura")
    if request.intent_id in state.cancellations:
        return state
    return replace(state, cancellations=state.cancellations + (request.intent_id,))


def reservations(
    state: ReserveState,
    context: RiskContext,
    policy: ReservationPolicy,
    at: datetime,
    leverage: Decimal,
) -> tuple[OrderMarginReservation, ...]:
    result = []
    with localcontext(ledger_context()):
        for order in sorted(state.orders, key=lambda o: o.order_id.int):
            entries = tuple(e for e in context.ledger.entries if e.order_id == order.order_id)
            filled = sum((e.quantity for e in entries), ZERO)
            remaining = order.requested_quantity.value - filled
            if remaining < 0 or filled > order.filled_quantity:
                raise ValueError("Cantidad ejecutada contradice snapshot de orden")
            reconciled = filled == order.filled_quantity
            accepted = order.status not in {OrderStatus.PENDING, OrderStatus.REJECTED}
            price = None
            if order.intent.order_type is OrderType.LIMIT:
                assert order.intent.limit_price is not None
                price = order.intent.limit_price.value
            elif policy.market_price_policy == "CAUSAL_FRESH_MARK":
                mark = context.mark
                if (
                    mark is not None
                    and mark.continuous
                    and mark.reference.provenance is not Origin.UNKNOWN
                    and mark.effective_at
                    <= at
                    <= mark.effective_at + timedelta(milliseconds=mark.freshness_limit_ms)
                ):
                    price = mark.price
            else:
                candidates = [
                    p
                    for p in state.prices
                    if p.order_id == order.order_id
                    and p.stamp.reference.provenance is not Origin.UNKNOWN
                    and p.stamp.received_at <= at
                    and p.stamp.effective_at
                    <= at
                    <= p.stamp.effective_at + timedelta(milliseconds=p.freshness_limit_ms)
                ]
                if candidates:
                    price = candidates[-1].price.value
            if not reconciled:
                hold = money(None, "ORDER_FILLS_NOT_RECONCILED")
            elif order.status in TERMINAL or not accepted:
                hold = money(ZERO)
            elif order.intent.side is Side.SELL and order.intent.reduce_only:
                hold = money(ZERO)
            elif order.intent.side is not Side.BUY or order.intent.reduce_only:
                hold = money(None, "UNSUPPORTED_EXPOSURE_SEMANTICS")
            elif price is None:
                hold = money(None, "CAUSAL_ORDER_PRICE_MISSING")
            else:
                hold = money(remaining * price / leverage)
            result.append(
                OrderMarginReservation(
                    order,
                    filled,
                    remaining,
                    price,
                    hold,
                    accepted,
                    order.intent_id in state.cancellations,
                    "RECONCILED" if reconciled else "PENDING_FILL_RECONCILIATION",
                    tuple(e.fill_id for e in entries),
                )
            )
    return tuple(result)
