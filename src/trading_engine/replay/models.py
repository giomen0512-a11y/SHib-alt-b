"""Snapshots integrales de una sesión offline; sin serialización de recovery."""

from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal

from trading_engine.binance.klines import minute_ms
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.orders import OrderCancelRequest, OrderIntent
from trading_engine.models.signals import Signal
from trading_engine.orchestration.models import CoordinatorState
from trading_engine.replay.clock import SimulatedClock
from trading_engine.replay.encoding import canonical, encoded
from trading_engine.replay.financial_ledger.models import LedgerReport, LedgerState
from trading_engine.replay.financial_performance.evidence import FinancialEvent
from trading_engine.replay.financial_performance.models import PerformanceReport, PerformanceState
from trading_engine.replay.isolated_margin_risk.evidence import RiskEvent
from trading_engine.replay.isolated_margin_risk.models import RiskReport, RiskState
from trading_engine.replay.ports import QuoteEvidence
from trading_engine.replay.reserve_transfer_simulation.models import ReserveReport, ReserveState
from trading_engine.replay.simulated_execution.state import ExecutionMetrics, ExecutionState
from trading_engine.strategy.alt_b.config import AltBConfig


@dataclass(frozen=True)
class ReplayRequest:
    start: datetime
    end: datetime
    allow_entries: bool = False

    def __post_init__(self) -> None:
        minute_ms(self.start)
        minute_ms(self.end)
        if self.end <= self.start:
            raise ValueError("Se requiere start < end")
        if type(self.allow_entries) is not bool:
            raise TypeError("allow_entries debe ser bool explícito")

    def warmup_start(self, config: AltBConfig) -> datetime:
        return self.start - timedelta(minutes=config.warmup_minutes)


@dataclass(frozen=True)
class InvalidPeriod:
    start: datetime
    end: datetime
    reason: str


@dataclass(frozen=True)
class ReplayEmission:
    at: datetime
    kind: Literal[
        "signal",
        "scheduled_action",
        "cancelled_action",
        "missing_quote",
        "order_intent",
        "cancel_request",
    ]
    payload: Signal | ScheduledAction | OrderIntent | OrderCancelRequest
    provenance: str | None = None


@dataclass(frozen=True)
class ReplayStats:
    minutes_read: int = 0
    warmup_read: int = 0
    warmup_applied: int = 0
    evaluated_read: int = 0
    checkpoints: int = 0
    first_read_open: datetime | None = None
    effective_start: datetime | None = None
    invalid_periods: tuple[InvalidPeriod, ...] = ()
    checkpoint_blocks: tuple[tuple[datetime, str], ...] = ()


@dataclass(frozen=True)
class ReplaySnapshot:
    request: ReplayRequest
    config: AltBConfig
    history_session: object
    quote_identity: str
    fact_identity: str
    clock: SimulatedClock
    next_open: datetime
    fact_cursor: int = 0
    activated: bool = False
    coordinator: CoordinatorState = CoordinatorState()
    stats: ReplayStats = ReplayStats()
    emissions: tuple[ReplayEmission, ...] = ()
    event_digest: str = "0" * 64
    event_count: int = 0
    interruption: str | None = None
    integrity_digest: str = ""
    execution_identity: str | None = None
    price_evidence: tuple[QuoteEvidence, ...] = ()
    simulation_identity: str | None = None
    simulation: ExecutionState | None = None
    ledger_identity: str | None = field(default=None, metadata={"omit_if_none": True})
    ledger: LedgerState | None = field(default=None, metadata={"omit_if_none": True})
    performance_identity: str | None = field(default=None, metadata={"omit_if_none": True})
    performance: PerformanceState | None = field(default=None, metadata={"omit_if_none": True})
    performance_cursor: int | None = field(default=None, metadata={"omit_if_none": True})
    performance_pending: tuple[tuple[str, FinancialEvent], ...] | None = field(
        default=None, metadata={"omit_if_none": True}
    )
    risk_identity: str | None = field(default=None, metadata={"omit_if_none": True})
    risk: RiskState | None = field(default=None, metadata={"omit_if_none": True})
    risk_cursor: int | None = field(default=None, metadata={"omit_if_none": True})
    risk_pending: tuple[RiskEvent, ...] | None = field(
        default=None, metadata={"omit_if_none": True}
    )
    reserve_identity: str | None = field(default=None, metadata={"omit_if_none": True})
    reserve: ReserveState | None = field(default=None, metadata={"omit_if_none": True})

    def calculate_integrity(self) -> str:
        """Checksum en memoria para detectar pérdida accidental de cualquier campo."""
        payload = tuple(
            (field.name, getattr(self, field.name))
            for field in fields(self)
            if field.name not in {"history_session", "integrity_digest"}
            and not (field.metadata.get("omit_if_none") and getattr(self, field.name) is None)
        )
        return sha256(encoded(payload)).hexdigest()


@dataclass(frozen=True)
class ReplayReport:
    market: str
    symbol: str
    interval: str
    requested_start: datetime
    requested_end: datetime
    processed_until: datetime
    stats: ReplayStats
    signals: int
    scheduled_actions: int
    cancelled_actions: int
    actions_without_quote: int
    fixture_intents: int
    order_intents: int
    cancel_requests: int
    final_state: CoordinatorState
    block_reason: str | None
    interruption: str | None
    event_digest: str
    event_count: int
    warmup_required: int = 1560
    financial_results: str = "FINANCIAL_RESULTS_NOT_AVAILABLE"
    execution_identity: str | None = None
    price_evidence: tuple[QuoteEvidence, ...] = ()
    modeled_intents: int = 0
    simulation: ExecutionState | None = None
    execution_metrics: ExecutionMetrics | None = None
    financial_ledger: LedgerReport | None = field(default=None, metadata={"omit_if_none": True})
    financial_performance: PerformanceReport | None = field(
        default=None, metadata={"omit_if_none": True}
    )
    isolated_margin_risk: RiskReport | None = field(default=None, metadata={"omit_if_none": True})
    reserve_transfer_simulation: ReserveReport | None = field(
        default=None, metadata={"omit_if_none": True}
    )

    def as_dict(self) -> dict[str, object]:
        result = canonical(self)
        assert isinstance(result, dict)
        return result
