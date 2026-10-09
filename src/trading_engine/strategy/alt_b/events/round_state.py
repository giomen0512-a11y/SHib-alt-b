"""Ronda LONG, escalera anclada y transición TP; exclusivamente hechos explícitos."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.identity import make_intent_id
from trading_engine.models.ids import RoundId
from trading_engine.models.orders import (
    Fill,
    OrderCancelRequest,
    OrderIntent,
    validate_cancel_requests,
)
from trading_engine.models.values import Price, Quantity
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.base import BaseState, base_intent
from trading_engine.strategy.alt_b.events.execution_state import OrderLeg, filled_quantity
from trading_engine.strategy.alt_b.math import (
    _context,
    dca_theoretical_price,
    order_notionals,
    take_profit_theoretical_price,
    weighted_average_fill_price,
)


def dca_intents(
    base: BaseState, p0: Price, at: datetime, config: AltBConfig
) -> tuple[OrderIntent, ...]:
    notionals = order_notionals(config)
    intents: list[OrderIntent] = []
    for k in range(1, 8):
        price = dca_theoretical_price(p0, k, config)
        with localcontext(_context()):
            qty = Quantity(notionals[k] / price.value)
        intents.append(
            OrderIntent(
                make_intent_id(
                    base.intent.round_id, f"ALT_B_DCA_{k}", at, str(base.intent.intent_id)
                ),
                base.intent.round_id,
                base.intent.symbol,
                Side.BUY,
                OrderType.LIMIT,
                qty,
                at,
                limit_price=price,
                reduce_only=False,
            )
        )
    return tuple(intents)


@dataclass(frozen=True)
class AltBRoundState:
    config: AltBConfig
    p0: Price
    opened_at: datetime
    dcas: tuple[OrderLeg, ...]
    take_profits: tuple[OrderLeg, ...] = ()
    cancel_requests: tuple[OrderCancelRequest, ...] = ()
    closing: bool = False
    closed_at: datetime | None = None
    reconciliation_reason: str | None = None

    def __post_init__(self) -> None:
        require_type(self.config, AltBConfig, "config")
        require_type(self.p0, Price, "p0")
        utc_timestamp(self.opened_at, "opened_at")
        require_type(self.dcas, tuple, "dcas")
        require_type(self.take_profits, tuple, "take_profits")
        require_type(self.closing, bool, "closing")
        if len(self.dcas) != 7:
            raise ValueError("La ronda requiere exactamente siete niveles DCA")
        for leg in self.legs:
            require_type(leg, OrderLeg, "leg")
            if (
                leg.intent.symbol != self.config.symbol
                or leg.intent.order_type is not OrderType.LIMIT
            ):
                raise ValueError("Orden de ronda incompatible")
        for leg in self.dcas:
            if leg.intent.side is not Side.BUY or leg.intent.reduce_only:
                raise ValueError("DCA requiere BUY sin reduce_only")
        for leg in self.take_profits:
            if leg.intent.side is not Side.SELL or not leg.intent.reduce_only:
                raise ValueError("TP requiere SELL reduce_only")
        potential_tps = tuple(
            leg
            for leg in self.take_profits
            if leg.order is None
            or leg.order.status
            not in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)
        )
        if len(potential_tps) > 1 and not any(leg.order_conflict for leg in potential_tps):
            raise ValueError("Snapshot con TP potencialmente activas incompatibles")
        if len({leg.intent.intent_id for leg in self.legs}) != len(self.legs):
            raise ValueError("IntentId duplicado en ronda")
        validate_cancel_requests(self.cancel_requests)
        for request in self.cancel_requests:
            if not any(leg.intent == request.intent for leg in self.legs):
                raise ValueError("Cancelación de intención ajena")
        if self.closed_at is not None:
            utc_timestamp(self.closed_at, "closed_at")
            if self.closed_at < self.opened_at or not self.closing:
                raise ValueError("Cierre de ronda incoherente")
        if self.reconciliation_reason not in (None, "late_execution_after_close"):
            raise ValueError("Bloqueo de reconciliación no reconocido")

    @property
    def legs(self) -> tuple[OrderLeg, ...]:
        return self.dcas + self.take_profits

    @property
    def round_id(self) -> RoundId:
        return self.dcas[0].intent.round_id

    def status(self, base: BaseState) -> str:
        if self.block_reason(base) is not None:
            return "RECONCILIATION"
        if self.closed_at is not None and self.can_close(base):
            return "CLOSED"
        return "CLOSING" if self.closing else "OPEN"

    def entry_fills(self, base: BaseState) -> tuple[Fill, ...]:
        return tuple(
            sorted(
                base.fills + tuple(f for leg in self.dcas for f in leg.fills),
                key=lambda f: f.fill_id.int,
            )
        )

    @property
    def exit_fills(self) -> tuple[Fill, ...]:
        return tuple(
            sorted((f for leg in self.take_profits for f in leg.fills), key=lambda f: f.fill_id.int)
        )

    def quantity_open(self, base: BaseState) -> Decimal:
        with localcontext(_context()):
            return filled_quantity(self.entry_fills(base)) - filled_quantity(self.exit_fills)

    def average_entry_price(self, base: BaseState) -> Price:
        return weighted_average_fill_price(self.entry_fills(base))

    def tp_target(self, base: BaseState) -> Price:
        return take_profit_theoretical_price(self.average_entry_price(base), self.config)

    def block_reason(self, base: BaseState) -> str | None:
        if self.reconciliation_reason:
            return self.reconciliation_reason
        if base.reconciliation_required or any(leg.reconciliation_required for leg in self.legs):
            return "execution_reconciliation_required"
        if self.quantity_open(base) < 0:
            return "exit_overfill"
        if any(
            leg.order is not None
            and leg.order.status is OrderStatus.REJECTED
            and not any(r.intent_id == leg.intent.intent_id for r in self.cancel_requests)
            for leg in self.take_profits
        ):
            return "tp_rejected_requires_reconciliation"
        return None

    def validate_base(self, base: BaseState) -> None:
        if base.intent != base_intent(base.action, base.reference_quote, self.config):
            raise ValueError("Presupuesto o intención Base incompatible con ronda")
        if base.p0 is not None and self.p0 != base.p0:
            raise ValueError("P0 no coincide con fills conciliados de Base")
        expected = dca_intents(base, self.p0, self.opened_at, self.config)
        if tuple(leg.intent for leg in self.dcas) != expected:
            raise ValueError("Escalera o identidad DCA incompatible con Base")
        if any(leg.intent.round_id != base.intent.round_id for leg in self.legs):
            raise ValueError("Order de otra ronda")
        fills = self.entry_fills(base) + self.exit_fills
        if len({f.fill_id for f in fills}) != len(fills):
            raise ValueError("FillId duplicado entre niveles")
        orders = tuple(leg.order for leg in self.legs if leg.order is not None)
        if base.order is not None:
            orders += (base.order,)
        if len({o.order_id for o in orders}) != len(orders):
            raise ValueError("OrderId vinculado a dos intenciones")
        if self.closed_at is not None and self.reconciliation_reason is None:
            if not self.can_close(base):
                raise ValueError("Cierre sin conciliación completa")

    def can_close(self, base: BaseState) -> bool:
        return (
            self.quantity_open(base) == 0
            and self.block_reason(base) is None
            and not base.order_pending
            and all(leg.settled for leg in self.legs)
        )

    def pending_cancellations(self) -> tuple[OrderCancelRequest, ...]:
        return tuple(
            r
            for r in self.cancel_requests
            if not next(leg for leg in self.legs if leg.intent == r.intent).settled
        )


@dataclass(frozen=True)
class RoundRecord:
    base: BaseState
    round_state: AltBRoundState

    def __post_init__(self) -> None:
        require_type(self.base, BaseState, "base")
        require_type(self.round_state, AltBRoundState, "round_state")
        if self.round_state.closed_at is None:
            raise ValueError("El historial requiere un cierre observado")
        self.round_state.validate_base(self.base)


def advance_round(
    base: BaseState,
    round_state: AltBRoundState | None,
    at: datetime,
    config: AltBConfig,
    *,
    context_blocked: bool = False,
) -> tuple[AltBRoundState | None, tuple[OrderIntent, ...], tuple[OrderCancelRequest, ...]]:
    """Planificar una vez; solicitar cancelación sin fingir confirmación."""
    intents: tuple[OrderIntent, ...] = ()
    if round_state is None:
        if context_blocked or base.p0 is None or base.block_reason != "base_confirmed":
            return None, (), ()
        intents = dca_intents(base, base.p0, at, config)
        round_state = AltBRoundState(config, base.p0, at, tuple(OrderLeg(i) for i in intents))
    round_state.validate_base(base)
    if round_state.closed_at is not None:
        return round_state, (), ()
    net = round_state.quantity_open(base)
    closing = (
        round_state.closing
        or net <= 0
        or any(
            leg.settled and leg.order is not None and leg.order.status is OrderStatus.FILLED
            for leg in round_state.take_profits
        )
    )
    round_state = replace(round_state, closing=closing)
    requests = list(round_state.cancel_requests)
    added: list[OrderCancelRequest] = []

    def cancel(leg: OrderLeg) -> None:
        # Una orden terminal puede esperar fills, pero no se finge que sea cancelable.
        if leg.order is not None and leg.order.status in (
            OrderStatus.CANCELED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
        ):
            return
        if not any(r.intent_id == leg.intent.intent_id for r in requests):
            request = OrderCancelRequest(leg.intent, at)
            requests.append(request)
            added.append(request)

    blocked = context_blocked or round_state.block_reason(base) is not None
    fatal = (
        context_blocked
        or round_state.reconciliation_reason is not None
        or base.reconciliation_reason is not None
        or base.executed_quantity > base.intent.quantity.value
        or any(
            leg.order_conflict or leg.executed_quantity > leg.intent.quantity.value
            for leg in round_state.legs
        )
        or round_state.block_reason(base)
        in ("exit_overfill", "tp_rejected_requires_reconciliation")
    )
    if fatal:
        for leg in round_state.legs:
            cancel(leg)
        return replace(round_state, cancel_requests=tuple(requests)), (), tuple(added)
    if closing:
        for leg in round_state.dcas:
            cancel(leg)
    target = round_state.tp_target(base)
    active = tuple(leg for leg in round_state.take_profits if not leg.settled)
    if active:
        for leg in active:
            with localcontext(_context()):
                remaining = leg.intent.quantity.value - leg.executed_quantity
            if remaining != net or leg.intent.limit_price != target or net <= 0:
                cancel(leg)
    elif not blocked and net > 0 and (not closing or all(leg.settled for leg in round_state.dcas)):
        # Todas las TP anteriores terminales y conciliadas antes de sustituir.
        entries = ",".join(str(f.fill_id) for f in round_state.entry_fills(base))
        exits = ",".join(str(f.fill_id) for f in round_state.exit_fills)
        previous = (
            str(round_state.take_profits[-1].intent.intent_id)
            if round_state.take_profits
            else "initial"
        )
        causal_ref = f"{base.intent.intent_id}:entries={entries}:exits={exits}:previous={previous}"
        intent = OrderIntent(
            make_intent_id(base.intent.round_id, "ALT_B_TP", at, causal_ref),
            base.intent.round_id,
            base.intent.symbol,
            Side.SELL,
            OrderType.LIMIT,
            Quantity(net),
            at,
            limit_price=target,
            reduce_only=True,
        )
        round_state = replace(
            round_state, take_profits=round_state.take_profits + (OrderLeg(intent),)
        )
        intents += (intent,)
    round_state = replace(round_state, cancel_requests=tuple(requests))
    if closing and round_state.can_close(base):
        round_state = replace(round_state, closed_at=at)
    return round_state, intents, tuple(added)
