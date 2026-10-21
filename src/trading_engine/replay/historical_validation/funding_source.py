"""Publicación opt-in de tasas fixture. No implementa FinancialSource ni pagos."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_engine.models._validation import nonblank, require_type
from trading_engine.utils.time import require_utc

from .audit import FundingInventory
from .funding_history import PreparedFundingHistory, PreparedFundingRow
from .parsers import FundingRate
from .query import HistoricalEvent
from .registry import digest
from .segments import RegistryProvenance
from .verification import VerificationMode

MAX_PUBLICATION_EVENTS = 10080


@dataclass(frozen=True)
class FundingPublicationPolicy:
    latency_ms: int
    max_events_per_read: int
    publication: str
    assumptions: str

    def __post_init__(self) -> None:
        if type(self.latency_ms) is not int or type(self.max_events_per_read) is not int:
            raise TypeError("Latencia y cuota funding requieren enteros")
        if self.latency_ms < 0:
            raise ValueError("FUNDING_LATENCY_INVALID")
        try:
            timedelta(milliseconds=self.latency_ms)
        except OverflowError as exc:
            raise ValueError("FUNDING_LATENCY_NOT_REPRESENTABLE") from exc
        if not 0 < self.max_events_per_read <= MAX_PUBLICATION_EVENTS:
            raise ValueError("FUNDING_PUBLICATION_LIMIT_INVALID")
        if self.publication != "SYNTHETIC_FIXTURE_ONLY":
            raise ValueError("FUNDING_PUBLICATION_NOT_AUTHORIZED")
        nonblank(self.assumptions, "assumptions")

    @property
    def identity(self) -> str:
        return "funding-publication-policy-v1:" + digest(self)


@dataclass(frozen=True)
class HistoricalFundingRateEvent:
    event_id: str
    sequence: int
    received_at: datetime
    rate: FundingRate
    historical_event: HistoricalEvent
    prepared_identity: str
    policy_identity: str
    verification_mode: VerificationMode
    verification_session_identity: str
    economic_use: str = "HISTORICAL_RATE_NOT_USER_PAYMENT"
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"


@dataclass(frozen=True)
class FundingInventoryPublication:
    inventory: FundingInventory
    inventory_identity: str
    received_at: datetime
    dataset_ids: tuple[str, ...]
    policy_identity: str
    prepared_identity: str
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
    economic_use: str = "DECLARED_RATE_INVENTORY_NOT_PAYMENT_COVERAGE"


@dataclass(frozen=True)
class HistoricalFundingSource:
    history: PreparedFundingHistory
    policy: FundingPublicationPolicy

    def __post_init__(self) -> None:
        require_type(self.history, PreparedFundingHistory, "history")
        require_type(self.policy, FundingPublicationPolicy, "policy")
        self.check()
        try:
            self.history.cutoff + timedelta(milliseconds=self.policy.latency_ms)
            inventory = self.history.evidence.inventory
            if inventory is not None:
                inventory.market_available_at + timedelta(milliseconds=self.policy.latency_ms)
        except OverflowError as exc:
            raise ValueError("FUNDING_PUBLICATION_TIME_OUT_OF_RANGE") from exc

    def check(self) -> None:
        self.history.check()
        if any(
            b.registration.provenance is not RegistryProvenance.FIXTURE
            for b in self.history.evidence.bindings
        ):
            raise ValueError("FUNDING_FINANCIAL_PUBLICATION_NOT_AUTHORIZED")

    @property
    def history_session(self) -> object:
        self.check()
        return self.history.session

    @property
    def identity(self) -> str:
        self.check()
        return "historical-funding-rates-v1:" + digest(
            (self.history.evidence.identity, self.policy)
        )

    def _eligible(self, cursor: int, until: datetime) -> Iterator[PreparedFundingRow]:
        require_utc(until)
        if not self.history.start <= until <= self.history.cutoff:
            raise ValueError("FUNDING_BEYOND_CUTOFF")
        latency = timedelta(milliseconds=self.policy.latency_ms)
        for row in self.history.read(cursor, until):
            if row.event.market_available_at + latency > until:
                break
            if row.event.provenance != (RegistryProvenance.FIXTURE.value,):
                raise ValueError("FUNDING_EVENT_PUBLICATION_NOT_AUTHORIZED")
            yield row

    def read(self, cursor: int, until: datetime) -> Iterator[HistoricalFundingRateEvent]:
        self.check()
        for count, _ in enumerate(self._eligible(cursor, until), 1):
            if count > self.policy.max_events_per_read:
                raise ValueError("FUNDING_PUBLICATION_LIMIT_EXCEEDED")
        for row in self._eligible(cursor, until):
            self.check()
            event = row.event
            if not isinstance(event.record, FundingRate):
                raise ValueError("FUNDING_RATE_REQUIRED")
            result = HistoricalFundingRateEvent(
                "historical-funding-rate-v1:" + digest((event.event_id, self.policy)),
                row.sequence - 1,
                event.market_available_at + timedelta(milliseconds=self.policy.latency_ms),
                event.record,
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
            yield result
        self.check()

    def inventory_at(self, until: datetime) -> FundingInventoryPublication | None:
        self.check()
        require_utc(until)
        if not self.history.start <= until <= self.history.cutoff:
            raise ValueError("FUNDING_BEYOND_CUTOFF")
        inventory = self.history.evidence.inventory
        if inventory is None:
            return None
        received_at = inventory.market_available_at + timedelta(milliseconds=self.policy.latency_ms)
        if received_at > until:
            return None
        identity = self.history.evidence.inventory_identity
        assert identity is not None
        result = FundingInventoryPublication(
            inventory,
            identity,
            received_at,
            tuple(b.dataset_id for b in self.history.evidence.bindings),
            self.policy.identity,
            self.history.evidence.identity,
        )
        self.check()
        return result

    def next_received_at(self, cursor: int) -> datetime | None:
        """Metadato de agenda de la preparación; no publica la tasa futura."""
        self.check()
        stream = self.history.read(cursor, self.history.cutoff)
        try:
            row = next(stream, None)
        finally:
            if hasattr(stream, "close"):
                stream.close()
        result = (
            None
            if row is None
            else row.event.market_available_at + timedelta(milliseconds=self.policy.latency_ms)
        )
        self.check()
        return result
