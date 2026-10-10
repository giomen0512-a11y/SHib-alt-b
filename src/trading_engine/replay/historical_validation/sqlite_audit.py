"""Auditoría del único esquema Futures aprobado, en transacción de sólo lectura."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, MARKET, minute_ms, to_ms
from trading_engine.market_data.audit import AuditReport, audit
from trading_engine.persistence.futures_sqlite import FuturesSQLite
from trading_engine.replay.encoding import encoded

from .contracts import DEFAULT_LIMITS, DatasetDeclaration, DatasetType, Limits


@dataclass(frozen=True)
class SQLiteManifestSummary:
    complete: int
    failed: int
    missing: int
    without_checksum: int
    malformed_checksum: int
    conflicts: int
    digest: str


@dataclass(frozen=True)
class SQLiteHistoricalAudit:
    dataset_id: str
    source_file: str
    coverage: AuditReport
    manifests: SQLiteManifestSummary
    outside_contract_rows: int
    causal_event_digest: str
    provider_authenticity: str = "NOT_AUTHENTICATED"
    causal_status: str = "BLOCKED_REQUIRES_SEPARATE_PREFIX_MANIFEST_REVIEW"
    limitations: tuple[str, ...] = (
        "SQLITE_CHECKSUM_FIELD_DOES_NOT_AUTHENTICATE_PROVIDER",
        "ORIGINAL_ARCHIVES_NOT_REVERIFIED",
        "RAW_BINANCE_CLOSE_TIME_NOT_STORED",
        "MANIFEST_COMPLETE_IS_NOT_TEMPORAL_COVERAGE",
        "MANIFEST_ACQUISITION_TIME_IS_NOT_MARKET_TIME",
    )

    @property
    def availability_status(self) -> str:
        if (
            not self.coverage.complete
            or self.outside_contract_rows
            or self.manifests.conflicts
            or self.manifests.malformed_checksum
        ):
            return "NOT_AVAILABLE"
        return "AVAILABLE_FOR_REVIEW_NOT_AUTHENTICATED"


def audit_sqlite(
    path: Path,
    declaration: DatasetDeclaration,
    *,
    as_of: datetime,
    limits: Limits = DEFAULT_LIMITS,
) -> SQLiteHistoricalAudit:
    import re

    if declaration.dataset_type is not DatasetType.TRADE:
        raise ValueError("SQLite aprobado contiene únicamente Trade klines 1m")
    if not path.is_file() or path.stat().st_size > limits.max_file_bytes:
        raise ValueError("SQLite no aportada/excede max_file_bytes")
    start, end = declaration.period_start, declaration.period_end
    if end > as_of:
        raise ValueError("Rango SQLite todavía no disponible")
    event_digest, manifest_digest = sha256(), sha256()
    with FuturesSQLite(path, read_only=True) as store:
        store.connection.execute("BEGIN")
        # Prefiltro acotado antes del auditor existente, cuyos gaps son acumulativos.
        previous = minute_ms(start)
        findings = 0
        for index, candle in enumerate(store.read(DEFAULT_SYMBOL, start, end), 1):
            if index > limits.max_rows:
                raise ValueError("SQLite excede max_rows")
            opened = to_ms(candle.open_time)
            if opened > previous:
                findings += 1
            if findings >= limits.max_findings:
                raise ValueError("SQLite excede max_findings")
            previous = opened + 60_000
            event_digest.update(encoded(candle))
        states = {"complete": 0, "failed": 0, "missing": 0}
        absent = malformed = conflicts = 0
        for index, row in enumerate(
            store.connection.execute(
                "SELECT file, period, checksum, source, state, processed_rows, "
                "processed_at_utc, error "
                "FROM import_manifest ORDER BY file"
            ),
            1,
        ):
            if index > limits.max_findings:
                raise ValueError("Manifiestos exceden max_findings")
            if row[4] not in states:
                raise ValueError("Estado de manifiesto inválido")
            states[row[4]] += 1
            absent += row[2] is None
            malformed += row[2] is not None and re.fullmatch("[a-fA-F0-9]{64}", row[2]) is None
            conflicts += row[7] is not None and "Conflicto" in row[7]
            manifest_digest.update(encoded(tuple(row)))
        outside = int(
            store.connection.execute(
                "SELECT count(*) FROM futures_klines_1m WHERE market != ? OR symbol != ? "
                "OR open_time_ms < ?",
                (MARKET, DEFAULT_SYMBOL.value, to_ms(LAUNCH)),
            ).fetchone()[0]
        )
        coverage = audit(store, DEFAULT_SYMBOL, start, end, as_of=as_of, require_taker_buy=True)
    return SQLiteHistoricalAudit(
        declaration.dataset_id,
        str(path.resolve()),
        coverage,
        SQLiteManifestSummary(
            states["complete"],
            states["failed"],
            states["missing"],
            absent,
            malformed,
            conflicts,
            manifest_digest.hexdigest(),
        ),
        outside,
        event_digest.hexdigest(),
    )
