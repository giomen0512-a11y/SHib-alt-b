"""Manifiestos y disponibilidad por métrica, nunca un READY global."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_engine.market_data.audit import Gap

from .contracts import Authentication, DatasetType, Readiness


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    dataset_type: DatasetType
    exchange: str
    market: str
    symbol: str
    contract_type: str
    settlement_asset: str
    interval: str
    period_start: datetime
    period_end: datetime
    source_url_declared: str
    source_documentation: str
    source_file: str
    archive_name: str | None
    sha256_calculated: str | None
    sha256_expected: str | None
    checksum_verification: str
    provider_authenticity: Authentication
    acquisition_time: datetime
    validation_time: datetime
    schema_version: str
    unit_contract: str
    row_count: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    data_gaps: tuple[Gap, ...]
    duplicates: int
    conflicts: int
    availability_status: str
    integrity_status: str
    coverage_status: str
    causal_status: str
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"

    def __post_init__(self) -> None:
        if self.provider_authenticity is Authentication.AUTHENTICATED:
            raise ValueError("Verificador independiente de autenticidad no habilitado")


@dataclass(frozen=True)
class MonthCoverage:
    month: str
    dataset_type: DatasetType
    start: datetime
    end: datetime
    expected_records: int | None
    present_records: int
    gaps: tuple[Gap, ...]
    duplicates: int
    conflicts: int
    coverage_percent: Decimal | None
    integrity: str
    authenticity: Authentication
    utilization: str


@dataclass(frozen=True)
class SemanticAudit:
    start: datetime
    end: datetime
    rows: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    duplicates: int
    conflicts: int
    errors: tuple[str, ...]
    gaps: tuple[Gap, ...]
    coverage_complete: bool
    causally_usable: bool
    event_digest: str
    funding_inventory_status: str


@dataclass(frozen=True)
class DatasetAudit:
    manifest: DatasetManifest
    semantic: SemanticAudit
    months: tuple[MonthCoverage, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class MetricReadiness:
    metric: str
    state: Readiness
    dataset_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HistoricalReadinessReport:
    start: datetime
    end: datetime
    metrics: tuple[MetricReadiness, ...]
    runtime: str = "STOPPED"
    survival: str = "NOT_CERTIFIED"
    financial_use: str = "REQUIRES_INDEPENDENT_PHASE_4A_5B_REVIEW"


def readiness_report(
    audits: tuple[DatasetAudit, ...],
    start: datetime,
    end: datetime,
) -> HistoricalReadinessReport:
    from trading_engine.utils.time import require_utc

    require_utc(start)
    require_utc(end)
    if start >= end:
        raise ValueError("Rango readiness inválido")
    metrics: list[MetricReadiness] = []
    categories = (
        ("TRADE_DATA_READINESS", DatasetType.TRADE),
        ("MARK_DATA_READINESS", DatasetType.MARK),
        ("FUNDING_DATA_READINESS", DatasetType.FUNDING),
        ("SYMBOL_RULES_READINESS", DatasetType.SYMBOL_RULES),
        ("BRACKETS_READINESS", DatasetType.BRACKETS),
        ("FEES_READINESS", DatasetType.FEES),
        ("EXECUTION_CALIBRATION_READINESS", DatasetType.EXECUTION),
    )
    for metric, category in categories:
        selected = tuple(a for a in audits if a.manifest.dataset_type is category)
        reasons: tuple[str, ...]
        if not selected:
            state, reasons = Readiness.MISSING, ("DATASET_NOT_PROVIDED",)
        elif any(a.semantic.conflicts or a.semantic.errors for a in selected):
            state, reasons = Readiness.CONFLICTED, ("DATA_OR_SCHEMA_CONFLICT",)
        elif not any(
            a.semantic.start <= start and a.semantic.end >= end and a.semantic.coverage_complete
            for a in selected
        ):
            # No concatena cobertura de archivos sin reconciliar solapamientos.
            reason = {
                DatasetType.FUNDING: "FUNDING_HISTORY_INCOMPLETE",
                DatasetType.BRACKETS: "BRACKET_HISTORY_NOT_AVAILABLE",
                DatasetType.FEES: "HISTORICAL_FEES_INCOMPLETE",
            }.get(category, "PERIOD_COVERAGE_NOT_PROVEN")
            state, reasons = Readiness.INCOMPLETE, (reason,)
        elif any(not a.semantic.causally_usable for a in selected):
            state, reasons = Readiness.NONCAUSAL, ("CAUSAL_CONTRACT_NOT_PROVEN",)
        elif any(
            a.manifest.provider_authenticity is not Authentication.AUTHENTICATED for a in selected
        ):
            state, reasons = Readiness.UNAUTHENTICATED, ("INDEPENDENT_AUTHENTICATION_MISSING",)
        else:
            state, reasons = Readiness.READY, ("READY_FOR_SEPARATE_REVIEW_ONLY",)
        metrics.append(
            MetricReadiness(metric, state, tuple(a.manifest.dataset_id for a in selected), reasons)
        )
    metrics.append(
        MetricReadiness(
            "LIQUIDATION_VALIDATION_READINESS",
            Readiness.INCOMPLETE,
            (),
            (
                "INTRAMINUTE_PATH_UNKNOWN",
                "EXCHANGE_LIQUIDATION_PRIORITY_NOT_VERIFIED",
                "RESERVES_AND_EXECUTION_NOT_VERIFIED",
            ),
        )
    )
    return HistoricalReadinessReport(start, end, tuple(metrics))
