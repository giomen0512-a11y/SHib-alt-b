"""Publicación opt-in de muestras Mark únicamente para fixtures autorizados."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_engine.binance.klines import DEFAULT_SYMBOL
from trading_engine.models._validation import nonblank, require_type
from trading_engine.replay.financial_performance.evidence import (
    FinancialEvent,
    MarkEvidence,
    Origin,
    Reference,
)
from trading_engine.utils.time import require_utc

from .mark_history import PreparedMarkHistory, PreparedMarkRow
from .parsers import MarkKline
from .query import HistoricalEvent
from .registry import digest
from .segments import RegistryProvenance
from .verification import VerificationMode

MAX_PUBLICATION_EVENTS = 10080


@dataclass(frozen=True)
class MarkPublicationPolicy:
    latency_ms: int
    freshness_limit_ms: int
    max_events_per_read: int
    publication: str
    assumptions: str

    def __post_init__(self) -> None:
        for name in ("latency_ms", "freshness_limit_ms", "max_events_per_read"):
            if type(getattr(self, name)) is not int:
                raise TypeError(name + " requiere entero")
        if self.latency_ms < 0 or not 0 <= self.freshness_limit_ms < 60000:
            raise ValueError("MARK_LATENCY_OR_FRESHNESS_INVALID")
        try:
            timedelta(milliseconds=self.latency_ms)
        except OverflowError as exc:
            raise ValueError("MARK_LATENCY_NOT_REPRESENTABLE") from exc
        if not 0 < self.max_events_per_read <= MAX_PUBLICATION_EVENTS:
            raise ValueError("MARK_PUBLICATION_LIMIT_INVALID")
        if self.publication != "SYNTHETIC_FIXTURE_ONLY":
            raise ValueError("MARK_PUBLICATION_NOT_AUTHORIZED")
        nonblank(self.assumptions, "assumptions")

    @property
    def identity(self) -> str:
        return "mark-publication-policy-v1:" + digest(self)


@dataclass(frozen=True)
class HistoricalMarkEvent(FinancialEvent):
    payload: MarkEvidence
    historical_event: HistoricalEvent
    prepared_identity: str
    policy_identity: str
    verification_mode: VerificationMode
    verification_session_identity: str
    authorization: str = "SYNTHETIC_FIXTURE_ONLY"


@dataclass(frozen=True)
class HistoricalMarkPriceSource:
    history: PreparedMarkHistory
    policy: MarkPublicationPolicy

    def __post_init__(self) -> None:
        require_type(self.history, PreparedMarkHistory, "history")
        require_type(self.policy, MarkPublicationPolicy, "policy")
        self.check()
        try:
            self.history.end + timedelta(milliseconds=self.policy.latency_ms)
            self.history.end + timedelta(milliseconds=self.policy.freshness_limit_ms)
        except OverflowError as exc:
            raise ValueError("MARK_PUBLICATION_TIME_OUT_OF_RANGE") from exc

    def check(self) -> None:
        self.history.check()
        if not self.history.row_count:
            raise ValueError("MARK_HISTORY_MISSING")
        if any(
            binding.registration.provenance is not RegistryProvenance.FIXTURE
            for binding in self.history.evidence.bindings
        ):
            raise ValueError("MARK_FINANCIAL_PUBLICATION_NOT_AUTHORIZED")

    @property
    def history_session(self) -> object:
        self.check()
        return self.history.session

    @property
    def identity(self) -> str:
        self.check()
        return "historical-mark-source-v1:" + digest((self.history.evidence.identity, self.policy))

    def _eligible(self, cursor: int, until: datetime) -> Iterator[PreparedMarkRow]:
        for row in self.history.read(cursor, until):
            if (
                row.event.market_available_at + timedelta(milliseconds=self.policy.latency_ms)
                <= until
            ):
                if row.event.provenance != (RegistryProvenance.FIXTURE.value,):
                    raise ValueError("MARK_EVENT_PUBLICATION_NOT_AUTHORIZED")
                yield row

    def read(self, cursor: int, until: datetime) -> Iterator[HistoricalMarkEvent]:
        self.check()
        require_utc(until)
        # Preflight acotado antes de entregar: el runner legado retiene todos los pendientes.
        for count, _ in enumerate(self._eligible(cursor, until), 1):
            if count > self.policy.max_events_per_read:
                raise ValueError("MARK_PUBLICATION_LIMIT_EXCEEDED")
        for row in self._eligible(cursor, until):
            self.check()
            event = row.event
            record = event.record
            if not isinstance(record, MarkKline):
                raise ValueError("MARK_RECORD_REQUIRED")
            at = event.market_available_at
            reference = Reference(
                "historical-mark-fixture:" + digest(event.sources),
                self.history.evidence.identity,
                Origin.FIXTURE,
                self.policy.assumptions + "; CLOSED_MARK_OHLC_1M; UNKNOWN_INTRAMINUTE_PATH",
            )
            payload = MarkEvidence(
                "historical-mark-close-v1:" + digest((event.event_id, self.policy)),
                at,
                DEFAULT_SYMBOL,
                record.close_sample(at),
                self.policy.freshness_limit_ms,
                row.continuous,
                reference,
            )
            yield HistoricalMarkEvent(
                at + timedelta(milliseconds=self.policy.latency_ms),
                row.sequence - 1,
                payload,
                event,
                self.history.evidence.identity,
                self.policy.identity,
                self.history.evidence.mode,
                (
                    "strict-preparation:" + self.history.evidence.identity
                    if self.history.evidence.verification is None
                    else self.history.evidence.verification.session_identity
                ),
            )
        self.check()
