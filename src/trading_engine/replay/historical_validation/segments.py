"""Contratos de registro explícitos; ninguna autorización financiera implícita."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trading_engine.models._validation import nonblank, require_type
from trading_engine.utils.time import require_utc

from .audit import FundingInventory
from .contracts import DEFAULT_LIMITS, DatasetDeclaration, Limits

SCHEMA_VERSION = 1
PARSER_VERSION = "historical-local-v1/4a5b-1"


class RegistryProvenance(StrEnum):
    FIXTURE = "SYNTHETIC_FIXTURE"
    DECLARED = "SOURCE_DECLARED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SegmentBounds:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        require_utc(self.start)
        require_utc(self.end)
        if self.start >= self.end:
            raise ValueError("Segmento vacío/invertido")


@dataclass(frozen=True)
class Registration:
    declaration: DatasetDeclaration
    original_file: str
    file_format: str
    validation_time: datetime
    provenance: RegistryProvenance
    provenance_evidence: str
    segments: tuple[SegmentBounds, ...]
    member: str | None = None
    checksum_file: str | None = None
    funding_inventory: FundingInventory | None = None
    limits: Limits = DEFAULT_LIMITS
    parser_version: str = PARSER_VERSION
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_type(self.declaration, DatasetDeclaration, "declaration")
        require_type(self.provenance, RegistryProvenance, "provenance")
        require_type(self.limits, Limits, "limits")
        require_utc(self.validation_time)
        nonblank(self.original_file, "original_file")
        nonblank(self.provenance_evidence, "provenance_evidence")
        if self.validation_time < self.declaration.acquisition_time:
            raise ValueError("Validación anterior a adquisición")
        if self.parser_version != PARSER_VERSION or self.schema_version != SCHEMA_VERSION:
            raise ValueError("Versión no implementada")
        if self.file_format not in {"csv", "json", "jsonl"}:
            raise ValueError("Formato local no implementado")
        if type(self.segments) is not tuple or not 0 < len(self.segments) <= 1000:
            raise ValueError("Segmentos explícitos acotados requeridos")
        cursor = self.declaration.period_start
        for segment in self.segments:
            require_type(segment, SegmentBounds, "segment")
            if self.declaration.interval == "1m":
                from trading_engine.binance.klines import minute_ms

                minute_ms(segment.start)
                minute_ms(segment.end)
            if segment.start != cursor or segment.end > self.declaration.period_end:
                raise ValueError("Segmentos deben particionar exactamente el período declarado")
            cursor = segment.end
        if cursor != self.declaration.period_end:
            raise ValueError("Falta segmento final")


@dataclass(frozen=True)
class RegisteredDataset:
    dataset_id: str
    audit_id: str
    source_hash: str
    row_count: int
    segment_ids: tuple[str, ...]
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"


@dataclass(frozen=True)
class SealedSegment:
    segment_id: str
    dataset_ids: tuple[str, ...]
    start: datetime
    end: datetime
    row_count: int
    digest: str
    coverage_status: str
    conflicts: int
    schema_version: int = SCHEMA_VERSION
