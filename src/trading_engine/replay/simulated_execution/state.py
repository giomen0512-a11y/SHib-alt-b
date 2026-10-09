"""Estado completo frozen: evidencia, colas temporales y asignación de volumen."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal

from trading_engine.models.enums import OrderStatus
from trading_engine.models.ids import IntentId
from trading_engine.models.market import Candle
from trading_engine.models.orders import Fill, Order, OrderCancelRequest
from trading_engine.replay.execution_model.contracts import price_context
from trading_engine.replay.ports import QuoteEvidence, ReplayFact
from trading_engine.replay.simulated_execution.policy import SimulationPolicy

TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)
PROVENANCE = "MODELED_EXECUTION"


@dataclass(frozen=True)
class SimulatedOrder:
    order: Order
    emitted_at: datetime
    acceptance_at: datetime
    eligible_from: datetime
    accepted_at: datetime | None = None
    fills: tuple[Fill, ...] = ()
    cancellation: OrderCancelRequest | None = None
    cancel_received_at: datetime | None = None
    cancel_confirm_at: datetime | None = None
    cancel_confirmed_at: datetime | None = None
    market_evaluated_at: datetime | None = None
    market_resolve_at: datetime | None = None
    market_resolved_at: datetime | None = None

    @property
    def remaining(self) -> Decimal:
        with localcontext(price_context()):
            return self.order.intent.quantity.value - self.order.filled_quantity


@dataclass(frozen=True)
class Transition:
    at: datetime
    intent_id: IntentId
    kind: Literal["accept", "cancel", "market_expire"]
    sequence: int


@dataclass(frozen=True)
class VolumeAllocation:
    intent_id: IntentId
    quantity: Decimal


@dataclass(frozen=True)
class MinuteLiquidity:
    candle: Candle
    cap: Decimal
    allocations: tuple[VolumeAllocation, ...] = ()

    @property
    def used(self) -> Decimal:
        with localcontext(price_context()):
            return sum((a.quantity for a in self.allocations), Decimal("0"))


@dataclass(frozen=True)
class ExecutionAudit:
    at: datetime
    kind: str
    intent_id: IntentId | None
    reason: str
    reference_candle: Candle | None = None
    quote: QuoteEvidence | None = None
    hypothesis: str = "NO_SEQUENCE_ASSUMED"
    queue_unknown: bool = True
    depth_unknown: bool = True
    provenance: str = PROVENANCE


@dataclass(frozen=True)
class ExecutionState:
    policy: SimulationPolicy
    identity: str
    orders: tuple[SimulatedOrder, ...] = ()
    transitions: tuple[Transition, ...] = ()
    deliveries: tuple[ReplayFact, ...] = ()
    facts: tuple[ReplayFact, ...] = ()
    audit: tuple[ExecutionAudit, ...] = ()
    liquidity: tuple[MinuteLiquidity, ...] = ()
    sequence: int = 0
    last_at: datetime | None = None
    last_minute_at: datetime | None = None


@dataclass(frozen=True)
class ExecutionMetrics:
    identity: str
    scenario: str
    intents_received: int
    orders_accepted: int
    orders_rejected: int
    orders_pending: int
    orders_partial: int
    orders_filled: int
    quantity_executed: Decimal
    orders_without_execution: int
    cancellations_requested: int
    cancellations_confirmed: int
    cancellations_pending: int
    fills_generated: int
    non_execution_reasons: tuple[tuple[str, int], ...]
    ambiguities: int
    volume: tuple[MinuteLiquidity, ...]
    reconciliation_conflicts: int
    provenance: str = PROVENANCE
