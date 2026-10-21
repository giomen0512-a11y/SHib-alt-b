"""Declaraciones fixture explícitas; no certificados de ejecución o mercado reales."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.ids import FillId
from trading_engine.replay.historical_validation.registry import digest
from trading_engine.utils.time import require_utc

FIXTURE_ONLY = "SYNTHETIC_FIXTURE_ONLY"
MAX_DECLARATIONS = 512


@dataclass(frozen=True)
class DynamicFundingPolicy:
    publication: str
    reference_semantics: str | None
    assumptions: str
    max_rates: int
    max_journal_entries: int
    max_fills: int
    max_window_minutes: int

    def __post_init__(self) -> None:
        if self.publication != FIXTURE_ONLY:
            raise ValueError("DYNAMIC_FUNDING_NOT_AUTHORIZED")
        if self.reference_semantics not in {None, "FIXTURE_MARK_AT_FUNDING_TIME"}:
            raise ValueError("DYNAMIC_FUNDING_REFERENCE_CONTRACT_REQUIRED")
        nonblank(self.assumptions, "assumptions")
        for name, ceiling in (
            ("max_rates", 512),
            ("max_journal_entries", 2048),
            ("max_fills", 4096),
            ("max_window_minutes", 44640),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("DYNAMIC_FUNDING_LIMIT_INVALID:" + name)

    @property
    def identity(self) -> str:
        return "dynamic-funding-policy-v1:" + digest(self)


@dataclass(frozen=True)
class FixtureExposureFinality:
    """Inventario independiente de TODOS los fills ejecutados <= through."""

    through: datetime
    received_at: datetime
    fact_identity: str
    fill_ids: tuple[FillId, ...]
    assumptions: str
    publication: str

    def __post_init__(self) -> None:
        require_utc(self.through)
        require_utc(self.received_at)
        if self.received_at < self.through or self.publication != FIXTURE_ONLY:
            raise ValueError("FIXTURE_FINALITY_NOT_AUTHORIZED_OR_FUTURE")
        nonblank(self.fact_identity, "fact_identity")
        nonblank(self.assumptions, "assumptions")
        require_type(self.fill_ids, tuple, "fill_ids")
        if len(self.fill_ids) > 4096 or len(set(self.fill_ids)) != len(self.fill_ids):
            raise ValueError("FIXTURE_FINALITY_FILL_IDS_INVALID")
        for fill_id in self.fill_ids:
            require_type(fill_id, UUID, "fill_id")
        object.__setattr__(self, "fill_ids", tuple(sorted(self.fill_ids, key=lambda i: i.int)))

    @property
    def identity(self) -> str:
        return "fixture-exposure-finality-v1:" + digest(self)


@dataclass(frozen=True)
class FixtureFinalitySource:
    events: tuple[FixtureExposureFinality, ...]

    def __post_init__(self) -> None:
        require_type(self.events, tuple, "events")
        if len(self.events) > MAX_DECLARATIONS:
            raise ValueError("FIXTURE_FINALITY_LIMIT")
        for event in self.events:
            require_type(event, FixtureExposureFinality, "finality")
        object.__setattr__(self, "events", tuple(sorted(self.events, key=lambda e: e.received_at)))

    @property
    def identity(self) -> str:
        return "fixture-finality-source-v1:" + digest(self.events)


@dataclass(frozen=True)
class FixtureFundingCalendar:
    """Calendario independiente completo de ocurrencias en [start,end] INCLUSIVO."""

    start: datetime
    end: datetime
    expected_timestamps: tuple[datetime, ...]
    received_at: datetime
    funding_economic_identity: str
    assumptions: str
    publication: str

    def __post_init__(self) -> None:
        for at in (self.start, self.end, self.received_at):
            require_utc(at)
        if self.start >= self.end or self.received_at < self.end:
            raise ValueError("FIXTURE_CLOSED_CALENDAR_NOT_AVAILABLE")
        if self.publication != FIXTURE_ONLY:
            raise ValueError("FIXTURE_CLOSED_CALENDAR_NOT_AUTHORIZED")
        nonblank(self.assumptions, "assumptions")
        nonblank(self.funding_economic_identity, "funding_economic_identity")
        require_type(self.expected_timestamps, tuple, "expected_timestamps")
        if len(self.expected_timestamps) > MAX_DECLARATIONS:
            raise ValueError("FIXTURE_CLOSED_CALENDAR_LIMIT")
        previous = None
        for at in self.expected_timestamps:
            require_utc(at)
            if not self.start <= at <= self.end or (previous is not None and at <= previous):
                raise ValueError("FIXTURE_CLOSED_CALENDAR_TIMESTAMPS_INVALID")
            previous = at

    @property
    def identity(self) -> str:
        return "fixture-closed-funding-calendar-v1:" + digest(self)

    @property
    def economic_key(self) -> tuple[datetime, datetime, tuple[datetime, ...]]:
        return self.start, self.end, self.expected_timestamps


@dataclass(frozen=True)
class FixtureCalendarSource:
    events: tuple[FixtureFundingCalendar, ...]

    def __post_init__(self) -> None:
        require_type(self.events, tuple, "events")
        if len(self.events) > MAX_DECLARATIONS:
            raise ValueError("FIXTURE_CALENDAR_SOURCE_LIMIT")
        for event in self.events:
            require_type(event, FixtureFundingCalendar, "calendar")
        object.__setattr__(self, "events", tuple(sorted(self.events, key=lambda e: e.received_at)))

    @property
    def identity(self) -> str:
        return "fixture-calendar-source-v1:" + digest(self.events)
