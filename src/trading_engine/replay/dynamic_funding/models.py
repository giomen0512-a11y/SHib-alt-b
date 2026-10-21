"""Journal acotado de decisiones; el dinero se calcula en FinancialPerformance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from trading_engine.models.ids import FillId, RoundId
from trading_engine.replay.financial_performance.evidence import FinancialEvent

if TYPE_CHECKING:
    from trading_engine.replay.historical_validation.funding_source import (
        FundingInventoryPublication,
        HistoricalFundingRateEvent,
    )

from .contracts import FixtureExposureFinality, FixtureFundingCalendar

DecisionStatus = Literal[
    "PENDING_EXPOSURE", "FINALIZED_EXPOSURE", "ASSIGNED", "NO_EXPOSURE", "BLOCKED"
]


@dataclass(frozen=True)
class FinalizationProof:
    identity: str
    through: datetime
    available_at: datetime
    fill_ids: tuple[FillId, ...]
    authority_identity: str
    kind: str


@dataclass(frozen=True)
class FundingDecision:
    identity: str
    rate_event: HistoricalFundingRateEvent
    received_at: datetime
    status: DecisionStatus
    reason: str
    dataset_identity: str
    policy_identity: str
    inventory_identity: str | None
    round_id: RoundId | None
    fill_ids: tuple[FillId, ...]
    quantity: Decimal | None
    reference_price: Decimal | None
    reference_at: datetime | None
    finalization: FinalizationProof | None
    payment: FinancialEvent | None


@dataclass(frozen=True)
class FundingState:
    identity: str
    economic_identity: str
    cursor: int = 0
    finality_cursor: int = 0
    calendar_cursor: int = 0
    pending: tuple[HistoricalFundingRateEvent, ...] = ()
    journal: tuple[FundingDecision, ...] = ()
    finalities: tuple[FixtureExposureFinality, ...] = ()
    calendars: tuple[FixtureFundingCalendar, ...] = ()
    inventory: FundingInventoryPublication | None = None
    coverage: tuple[FinancialEvent, ...] = ()

    @property
    def events(self) -> tuple[FinancialEvent, ...]:
        return tuple(
            sorted(
                tuple(d.payment for d in self.journal if d.payment is not None) + self.coverage,
                key=lambda e: e.key,
            )
        )

    @property
    def latest(self) -> tuple[FundingDecision, ...]:
        result: dict[str, FundingDecision] = {}
        for decision in self.journal:
            result[decision.rate_event.event_id] = decision
        return tuple(result.values())


@dataclass(frozen=True)
class FundingReport:
    identity: str
    economic_identity: str
    cursor: int
    decisions: tuple[FundingDecision, ...]
    journal: tuple[FundingDecision, ...]
    coverage: tuple[FinancialEvent, ...]
    status: str
    reasons: tuple[str, ...]
    authentication: str = "NOT_AUTHENTICATED"
    authorization: str = "SYNTHETIC_FIXTURE_ONLY"
    economic_use: str = "MODELED_FIXTURE_ASSIGNMENTS_NOT_USER_PAYMENTS"
