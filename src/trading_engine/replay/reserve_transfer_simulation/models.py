"""Estado inmutable y auditable de reservas y transferencias offline."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_engine.models.ids import FillId, IntentId, OrderId, RoundId
from trading_engine.models.orders import Order
from trading_engine.replay.financial_performance.models import Metric
from trading_engine.replay.isolated_margin_risk.evidence import MarginAdjustmentEvidence, RiskStamp
from trading_engine.replay.isolated_margin_risk.models import RiskEvaluation
from trading_engine.replay.reserve_transfer_simulation.evidence import (
    CapitalPartitionEvidence,
    OrderPriceEvidence,
    ReserveEvent,
)


@dataclass(frozen=True)
class CapitalPartition:
    account_wallet: Metric
    free_balance: Metric
    isolated_wallet_allocated: Metric
    pending_order_holds: Metric
    pending_transfer_holds: Metric
    other_accounted_buckets: Decimal
    status: str


@dataclass(frozen=True)
class OrderMarginReservation:
    order: Order
    filled_quantity: Decimal
    remaining_quantity: Decimal
    relevant_price: Decimal | None
    hold: Metric
    accepted: bool
    cancel_requested: bool
    reconciliation: str
    fill_ids: tuple[FillId, ...]

    @property
    def order_id(self) -> OrderId:
        return self.order.order_id

    @property
    def intent_id(self) -> IntentId:
        return self.order.intent_id

    @property
    def round_id(self) -> RoundId:
        return self.order.round_id


@dataclass(frozen=True)
class TransferRequest:
    request_id: str
    hold_id: str
    round_id: RoundId
    amount_usdc: Decimal
    held_usdc: Decimal
    confirmed_usdc: Decimal
    state: str
    proposed_at: datetime
    request_at: datetime
    scheduled_confirmation_at: datetime
    expires_at: datetime
    attempts: int
    buffer_before: Decimal | None
    balance_before: CapitalPartition
    first_breach_at: datetime | None = None
    first_breach_buffer: Decimal | None = None
    confirmed_at: datetime | None = None
    terminal_at: datetime | None = None
    rejection_reason: str | None = None
    buffer_after: Decimal | None = None
    balance_after: CapitalPartition | None = None
    confirmation: MarginAdjustmentEvidence | None = None
    risk_outcome: str = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class ScheduledTransfer:
    scheduled_at: datetime
    sequence: int
    request_id: str
    kind: str

    @property
    def key(self) -> tuple[datetime, int, str]:
        return self.scheduled_at, self.sequence, self.request_id


@dataclass(frozen=True)
class ReserveAudit:
    stamp: RiskStamp
    kind: str
    round_id: RoundId | None
    payload: object


@dataclass(frozen=True)
class ReserveDecision:
    at: datetime
    round_id: RoundId
    status: str
    reason: str
    required_usdc: Decimal | None
    request_id: str | None = None


@dataclass(frozen=True)
class ReserveState:
    identity: str
    capital: CapitalPartitionEvidence | None
    partition: CapitalPartition
    orders: tuple[Order, ...] = ()
    cancellations: tuple[IntentId, ...] = ()
    prices: tuple[OrderPriceEvidence, ...] = ()
    reservations: tuple[OrderMarginReservation, ...] = ()
    transfers: tuple[TransferRequest, ...] = ()
    queue: tuple[ScheduledTransfer, ...] = ()
    adjustments: tuple[MarginAdjustmentEvidence, ...] = ()
    processed_event_ids: tuple[str, ...] = ()
    processed_fill_ids: tuple[FillId, ...] = ()
    source_cursor: int = 0
    journal: tuple[ReserveAudit, ...] = ()
    decisions: tuple[ReserveDecision, ...] = ()
    risk: RiskEvaluation | None = None
    samples: tuple[RiskEvaluation, ...] = ()
    conflicts: tuple[str, ...] = ()
    last_at: datetime | None = None
    initialized: bool = False
    settled_partition: CapitalPartition | None = None
    received_events: tuple[ReserveEvent, ...] = ()


@dataclass(frozen=True)
class ReserveReport:
    identity: str
    state: ReserveState
    interval_path: str = "UNKNOWN_INTRAMINUTE_PATH"
    liquidation: str = "NO_LIQUIDATION_SIMULATED"
    runtime: str = "STOPPED"
    trajectory: str = "SAMPLED_COUNTERFACTUAL_ONLY"
