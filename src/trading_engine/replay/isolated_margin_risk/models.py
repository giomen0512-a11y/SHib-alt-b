"""Estado/journal reconstruibles, sin comandos de ejecución o transferencia."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_engine.models.ids import FillId, RoundId
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_performance.evidence import MarkEvidence
from trading_engine.replay.financial_performance.models import Metric, Valuation
from trading_engine.replay.isolated_margin_risk.evidence import RiskEvent
from trading_engine.replay.isolated_margin_risk.policy import MarginAllocationPolicy


@dataclass(frozen=True)
class RiskContext:
    at: datetime
    ledger: LedgerState
    financial: Valuation
    mark: MarkEvidence | None
    performance_identity: str
    performance_journal_cursor: int


@dataclass(frozen=True)
class MarginTransition:
    fill_id: FillId
    round_id: RoundId
    at: datetime
    assigned_usdc: Decimal
    released_usdc: Decimal
    initial_principal_after_usdc: Decimal


@dataclass(frozen=True)
class MarginHypothesis:
    evidence_id: str
    additional_margin: Metric
    buffer_before: Metric
    buffer_after: Metric
    margin_balance_after: Metric
    threshold_before: Metric
    threshold_after: Metric
    required_for_target: Metric
    reserve_required: Metric
    feasibility: str
    target_met_by_hypothesis: bool | None
    label: str = "HYPOTHETICAL_ONLY_NO_TRANSFER"


@dataclass(frozen=True)
class IsolatedPositionRisk:
    round_id: RoundId
    fill_ids: tuple[FillId, ...]
    open_quantity: Decimal
    open_cost_basis_usdc: Decimal
    initial_assigned: Metric
    initial_released: Metric
    additional_assigned: Metric
    withdrawn: Metric
    sell_adjustments: Metric
    allocated_fees: Metric
    allocated_funding: Metric
    isolated_wallet_balance: Metric
    position_notional: Metric
    unrealized: Metric
    isolated_margin_balance: Metric
    maintenance: Metric
    buffer: Metric
    margin_ratio: Metric
    threshold_price: Metric
    distance_to_threshold: Metric
    condition: str
    availability: str
    mark_id: str | None
    schedule_id: str | None
    transitions: tuple[MarginTransition, ...]
    hypothesis: MarginHypothesis | None
    pending_order_reserve: str = "NOT_AVAILABLE"
    free_capital: str = "NOT_AVAILABLE"
    threshold_label: str = "MODELED_MAINTENANCE_THRESHOLD_PRICE"


@dataclass(frozen=True)
class RiskEvaluation:
    at: datetime
    positions: tuple[IsolatedPositionRisk, ...]
    account_wallet: Metric
    account_equity: Metric
    sampling: str = "SAMPLED_INSTANT_ONLY_INTRAMINUTE_UNKNOWN"
    liquidation: str = "NOT_SIMULATED"
    survival: str = "NOT_DETERMINABLE_FROM_SAMPLES"


@dataclass(frozen=True)
class RiskObservation:
    at: datetime
    payload: RiskContext | RiskEvent | str


@dataclass(frozen=True)
class RiskConflict:
    at: datetime
    reason: str
    observation: RiskObservation


@dataclass(frozen=True)
class RiskState:
    identity: str
    policy: MarginAllocationPolicy
    period_start: datetime
    period_end: datetime
    context: RiskContext | None = None
    events: tuple[RiskEvent, ...] = ()
    journal: tuple[RiskObservation, ...] = ()
    evaluations: tuple[RiskEvaluation, ...] = ()
    conflicts: tuple[RiskConflict, ...] = ()
    last_at: datetime | None = None


@dataclass(frozen=True)
class RiskReport:
    identity: str
    policy: MarginAllocationPolicy
    source_identity: str
    current: RiskEvaluation
    evaluations: tuple[RiskEvaluation, ...]
    journal: tuple[RiskObservation, ...]
    processed_event_ids: tuple[str, ...]
    conflicts: tuple[RiskConflict, ...]
    sampled_breaches: int
    information: str
    interval_risk: str = "POTENTIAL_BREACH_BETWEEN_SAMPLES_UNKNOWN"
    runtime: str = "STOPPED"
