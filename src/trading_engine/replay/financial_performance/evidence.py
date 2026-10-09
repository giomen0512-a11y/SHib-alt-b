"""Contratos monetarios offline: ocurrencia, disponibilidad y autenticidad separadas."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from trading_engine.models._validation import finite_decimal, nonblank, require_type
from trading_engine.models.ids import FillId, OrderId, RoundId
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.utils.time import require_utc


class Origin(StrEnum):
    OBSERVED = "OBSERVED"
    MODELED = "MODELED"
    FIXTURE = "SYNTHETIC_FIXTURE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Reference:
    source: str
    dataset: str
    provenance: Origin
    assumptions: str

    def __post_init__(self) -> None:
        for name in ("source", "dataset", "assumptions"):
            nonblank(getattr(self, name), name)
        require_type(self.provenance, Origin, "provenance")
        if self.provenance is Origin.OBSERVED:
            raise ValueError(
                "OBSERVED requiere validación auténtica independiente; modo no autorizado"
            )


@dataclass(frozen=True)
class FeeEvidence:
    event_id: str
    fill_id: FillId
    order_id: OrderId
    effective_at: datetime
    amount: Decimal
    currency: str
    reference: Reference

    def __post_init__(self) -> None:
        require_type(self.fill_id, UUID, "fill_id")
        require_type(self.order_id, UUID, "order_id")
        finite_decimal(self.amount, "fee", nonnegative=True)
        nonblank(self.currency, "currency")
        validate_event(self)


@dataclass(frozen=True)
class FundingEvidence:
    event_id: str
    symbol: Symbol
    effective_at: datetime
    round_id: RoundId
    exposure_quantity: Decimal
    reference: Reference
    amount_usdc: Decimal | None = None
    rate: Decimal | None = None
    reference_price: Decimal | None = None
    reference_price_at: datetime | None = None
    positive_rate_pays: bool | None = None

    def __post_init__(self) -> None:
        validate_event(self)
        require_type(self.symbol, Symbol, "symbol")
        require_type(self.round_id, UUID, "round_id")
        finite_decimal(self.exposure_quantity, "exposure_quantity", nonnegative=True)
        if self.amount_usdc is not None:
            finite_decimal(self.amount_usdc, "amount_usdc")
            if any(
                v is not None
                for v in (
                    self.rate,
                    self.reference_price,
                    self.reference_price_at,
                    self.positive_rate_pays,
                )
            ):
                raise ValueError("Funding explícito y modelado son modalidades separadas")
        else:
            if self.rate is None or self.reference_price is None:
                raise ValueError("Funding modelado exige tasa y precio de referencia")
            finite_decimal(self.rate, "rate")
            finite_decimal(self.reference_price, "reference_price")
            assert self.reference_price is not None
            if self.reference_price <= 0:
                raise ValueError("Precio funding debe ser positivo")
            if self.reference_price_at is None:
                raise ValueError("Falta instante de referencia funding")
            require_utc(self.reference_price_at)
            if self.reference_price_at != self.effective_at:
                raise ValueError("Referencia funding debe corresponder al instante efectivo")
            if self.positive_rate_pays is not True:
                raise ValueError("El contrato LONG debe declarar que tasa positiva implica pago")
            if self.reference.provenance is not Origin.MODELED:
                raise ValueError("Funding calculado debe etiquetarse MODELED")


@dataclass(frozen=True)
class TransferEvidence:
    event_id: str
    effective_at: datetime
    amount_usdc: Decimal
    reference: Reference
    kind: str

    def __post_init__(self) -> None:
        validate_event(self)
        finite_decimal(self.amount_usdc, "transfer")
        if self.kind not in {"DEPOSIT", "WITHDRAWAL"} or self.amount_usdc <= 0:
            raise ValueError("Movimiento externo debe tener tipo y cantidad positiva")


@dataclass(frozen=True)
class MarkEvidence:
    event_id: str
    effective_at: datetime
    symbol: Symbol
    price: Decimal
    freshness_limit_ms: int
    continuous: bool
    reference: Reference
    price_kind: str = "MARK"

    def __post_init__(self) -> None:
        validate_event(self)
        require_type(self.symbol, Symbol, "symbol")
        finite_decimal(self.price, "mark")
        if self.price <= 0 or self.price_kind != "MARK":
            raise ValueError("Exigir precio Mark positivo explícito; Close/Quote no son Mark")
        if type(self.freshness_limit_ms) is not int or self.freshness_limit_ms < 0:
            raise ValueError("Vigencia Mark debe ser entero no negativo")
        try:
            self.effective_at + timedelta(milliseconds=self.freshness_limit_ms)
        except OverflowError as exc:
            raise ValueError("Vigencia Mark no representable") from exc
        require_type(self.continuous, bool, "continuous")


@dataclass(frozen=True)
class CoverageEvidence:
    """Declaración explícita de inventario completo, nunca inferida de ausencia."""

    event_id: str
    effective_at: datetime
    start: datetime
    end: datetime
    metric: str
    event_ids: tuple[str, ...]
    reference: Reference

    def __post_init__(self) -> None:
        validate_event(self)
        require_utc(self.start)
        require_utc(self.end)
        if self.start >= self.end or self.effective_at != self.end:
            raise ValueError("Cobertura exige [start,end] y publicación después de end")
        if self.metric not in {"FUNDING", "TRANSFERS"}:
            raise ValueError("Inventarios implementados: FUNDING/TRANSFERS")
        require_type(self.event_ids, tuple, "event_ids")
        for event_id in self.event_ids:
            nonblank(event_id, "event_id")
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("Inventario duplicado")
        object.__setattr__(self, "event_ids", tuple(sorted(self.event_ids)))


FinancialPayload = (
    FeeEvidence | FundingEvidence | TransferEvidence | MarkEvidence | CoverageEvidence
)


def validate_event(value: FinancialPayload) -> None:
    nonblank(value.event_id, "event_id")
    require_utc(value.effective_at)
    require_type(value.reference, Reference, "reference")


@dataclass(frozen=True)
class FinancialEvent:
    received_at: datetime
    sequence: int
    payload: FinancialPayload

    def __post_init__(self) -> None:
        require_utc(self.received_at)
        if not isinstance(
            self.payload,
            (FeeEvidence, FundingEvidence, TransferEvidence, MarkEvidence, CoverageEvidence),
        ):
            raise TypeError("Evidencia financiera inválida")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("Secuencia financiera debe ser entero no negativo")
        if self.payload.effective_at > self.received_at:
            raise ValueError("Evidencia financiera futura respecto a recepción")

    @property
    def key(self) -> tuple[datetime, int]:
        return self.received_at, self.sequence


class FinancialSource(Protocol):
    @property
    def identity(self) -> str: ...

    def read(self, cursor: int, until: datetime) -> Iterator[FinancialEvent]: ...


class MarkPriceSource(FinancialSource, Protocol):
    """Fuente independiente: nunca deriva Mark automáticamente de trades/bid/ask."""


@dataclass(frozen=True)
class FixtureFinancialSource:
    events: tuple[FinancialEvent, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.events, tuple, "events")
        for event in self.events:
            require_type(event, FinancialEvent, "event")
        if len({e.sequence for e in self.events}) != len(self.events):
            raise ValueError("Secuencias de fuente duplicadas")
        if any(e.payload.reference.provenance is not Origin.FIXTURE for e in self.events):
            raise ValueError("Fuente fixture exige SYNTHETIC_FIXTURE")
        object.__setattr__(self, "events", tuple(sorted(self.events, key=lambda e: e.key)))

    @property
    def identity(self) -> str:
        return "fixture-financial-source-v1:" + sha256(encoded(self.events)).hexdigest()

    def read(self, cursor: int, until: datetime) -> Iterator[FinancialEvent]:
        for event in self.events[cursor:]:
            if event.received_at > until:
                break
            yield event


@dataclass(frozen=True)
class FixtureMarks(FixtureFinancialSource):
    def __post_init__(self) -> None:
        super().__post_init__()
        if any(not isinstance(e.payload, MarkEvidence) for e in self.events):
            raise ValueError("MarkPriceSource solo proporciona MarkEvidence")
