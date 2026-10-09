"""Estado/journal frozen y disponibilidad independiente para cada métrica."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_engine.models.ids import RoundId
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_performance.evidence import FinancialEvent, Origin
from trading_engine.replay.financial_performance.policy import PerformancePolicy


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    status: str
    reasons: tuple[str, ...] = ()
    provenances: tuple[Origin, ...] = ()


@dataclass(frozen=True)
class RoundPerformance:
    round_id: RoundId
    exposure: Decimal
    gross_realized: Metric
    fees: Metric
    funding: Metric
    net_cash: Metric
    net_realized_closed: Metric


@dataclass(frozen=True)
class Valuation:
    at: datetime
    gross_realized: Metric
    fees: Metric
    funding: Metric
    deposits: Metric
    withdrawals: Metric
    net_cash: Metric
    unrealized: Metric
    wallet: Metric
    equity: Metric
    mark_id: str | None
    wallet_negative: bool
    rounds: tuple[RoundPerformance, ...]


@dataclass(frozen=True)
class FinancialObservation:
    at: datetime
    payload: LedgerState | FinancialEvent | str  # VALUE es muestreo causal explícito.


@dataclass(frozen=True)
class PerformanceConflict:
    at: datetime
    reason: str
    observation: FinancialObservation


@dataclass(frozen=True)
class PerformanceState:
    identity: str
    policy: PerformancePolicy
    period_start: datetime
    period_end: datetime
    ledger: LedgerState | None = None
    events: tuple[FinancialEvent, ...] = ()
    journal: tuple[FinancialObservation, ...] = ()
    series: tuple[Valuation, ...] = ()
    conflicts: tuple[PerformanceConflict, ...] = ()
    peak_equity: Decimal | None = None
    peak_at: datetime | None = None
    max_drawdown: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_peak_at: datetime | None = None
    max_valley_at: datetime | None = None
    invalid_samples: int = 0
    last_at: datetime | None = None


@dataclass(frozen=True)
class PerformanceReport:
    identity: str
    period_start: datetime
    period_end: datetime
    policy: PerformancePolicy
    source_identity: str
    mark_identity: str
    current: Valuation
    series: tuple[Valuation, ...]
    peak_equity: Decimal | None
    peak_at: datetime | None
    drawdown_usdc: Metric
    drawdown_pct: Metric
    max_drawdown_usdc: Metric
    max_drawdown_pct: Metric
    max_peak_at: datetime | None
    max_valley_at: datetime | None
    valuation_coverage: str
    coverage_event_ids: tuple[str, ...]
    processed_event_ids: tuple[str, ...]
    journal: tuple[FinancialObservation, ...]
    conflicts: tuple[PerformanceConflict, ...]
    general_return_pct: str = "NOT_AVAILABLE"
    margin_available: str = "NOT_AVAILABLE"
    liquidation: str = "NOT_AVAILABLE"
    add_margin: str = "NOT_AVAILABLE"
