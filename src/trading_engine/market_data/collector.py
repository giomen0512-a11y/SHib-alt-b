"""Operación autónoma finita del pipeline existente; sin estrategia ni ejecución."""

import json
import logging
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_engine.binance.klines import (
    DEFAULT_SYMBOL,
    LAUNCH,
    MARKET,
    MINUTE_MS,
    from_ms,
    minute_ms,
    reference_ms,
)
from trading_engine.binance.public_data import PublicHTTP
from trading_engine.market_data.audit import AuditReport, audit
from trading_engine.market_data.history import PublicDataClient, backfill, sync_range
from trading_engine.models.values import Symbol
from trading_engine.persistence.collector_progress import (
    begin_attempt,
    clear_job_error,
    finish_error,
    job_errors,
    load_progress,
    pending_gaps,
    record_audit,
    save_progress,
)
from trading_engine.persistence.data_safety import (
    DEFAULT_RESERVE_BYTES,
    consistent_backup,
    free_bytes,
    require_space,
)
from trading_engine.persistence.futures_sqlite import FuturesSQLite
from trading_engine.utils.time import require_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectorConfig:
    db_path: Path
    symbol: Symbol = DEFAULT_SYMBOL
    publication_delay_seconds: int = 5
    max_update_minutes: int = 1500
    min_free_bytes: int = DEFAULT_RESERVE_BYTES
    request_interval_seconds: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.db_path, Path) or self.symbol != DEFAULT_SYMBOL:
            raise ValueError("Colector Fase Datos 2: Path y símbolo 1000SHIBUSDC requeridos")
        for name, low, high in (
            ("publication_delay_seconds", 0, 300),
            ("max_update_minutes", 1, 10080),
            ("min_free_bytes", 0, 58 * 1024**3),
            ("request_interval_seconds", 1, 60),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name}: entero fuera del rango {low}..{high}")


def eligible_end(config: CollectorConfig, as_of: datetime) -> datetime:
    reference = require_utc(as_of) - timedelta(seconds=config.publication_delay_seconds)
    return from_ms(reference_ms(reference) // MINUTE_MS * MINUTE_MS)


def _event(event: str, **values: object) -> None:
    logger.info(json.dumps({"timestamp": datetime.now(UTC).isoformat(), "event": event, **values}))


class _SpaceCheckedClient:
    def __init__(self, client: PublicDataClient, config: CollectorConfig) -> None:
        self.client, self.config = client, config

    def _check(self) -> None:
        require_space(self.config.db_path, self.config.min_free_bytes)

    def download(self, url: str, target: Path) -> None:
        self._check()
        require_space(target.parent, self.config.min_free_bytes)
        self.client.download(url, target)

    def text(self, url: str) -> str:
        self._check()
        return self.client.text(url)

    def klines(
        self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
    ) -> list[list[object]]:
        self._check()
        return self.client.klines(symbol, start_ms, end_ms, limit)


def _check_integrity(store: FuturesSQLite) -> None:
    errors = store.integrity_errors()
    if errors:
        raise ValueError(f"SQLite corrupto: {'; '.join(errors)}")


def _history(
    store: FuturesSQLite,
    client: PublicDataClient,
    config: CollectorConfig,
    end: datetime,
    as_of: datetime,
) -> AuditReport:
    _check_integrity(store)
    current = LAUNCH
    while current < end:
        first = current.replace(day=1, hour=0, minute=0)
        next_month = first.replace(
            year=first.year + (first.month == 12), month=first.month % 12 + 1
        )
        upper = min(end, next_month)
        period = first.strftime("%Y-%m")
        _event(
            "history_period_start", period=period, start=current.isoformat(), end=upper.isoformat()
        )
        report = backfill(
            store,
            client,
            config.symbol,
            current,
            upper,
            as_of=as_of,
            require_checksum=True,
            check_database=False,
        )
        _event(
            "history_period_finished",
            period=period,
            rows=report.rows,
            missing_minutes=sum(g.missing_minutes for g in report.gaps),
            errors=report.errors,
        )
        current = upper
    return audit(store, config.symbol, LAUNCH, end, as_of=as_of, require_taker_buy=True)


def _update(
    store: FuturesSQLite,
    client: PublicDataClient,
    config: CollectorConfig,
    target: datetime,
    as_of: datetime,
) -> AuditReport | None:
    progress = load_progress(store, config.symbol)
    if progress is None or not progress.ready:
        raise ValueError(
            "Histórico sin validar: ejecutar history-run/audit-full antes de update-once"
        )
    cursor = progress.validated_end_ms
    target_ms = minute_ms(target)
    if cursor > target_ms:
        raise ValueError("Reloj/retraso anterior al checkpoint validado; no se ingieren velas")
    latest = store.latest_open(config.symbol)
    if latest is None or latest + MINUTE_MS < cursor:
        raise ValueError("Checkpoint no respaldado por SQLite; requiere auditoría completa")
    # Una sola vela de frontera detecta corrupción local sin recorrer años cada minuto.
    if cursor > progress.start_ms:
        boundary = tuple(store.candles(config.symbol, cursor - MINUTE_MS, cursor))
        if len(boundary) != 1 or boundary[0].taker_buy_base_volume is None:
            raise ValueError("Frontera de continuidad inválida")
    if cursor == target_ms:
        save_progress(
            store,
            config.symbol,
            replace(
                progress,
                last_state="complete",
                last_success_utc=datetime.now(UTC).isoformat(),
                last_error=None,
                audit_complete=True,
            ),
        )
        return None
    end_ms = min(target_ms, cursor + config.max_update_minutes * MINUTE_MS)
    _event(
        "update_range",
        start_ms=cursor,
        end_ms=end_ms,
        target_end_ms=target_ms,
        latest_persisted_open_ms=latest,
    )
    before = audit(
        store,
        config.symbol,
        from_ms(cursor),
        from_ms(end_ms),
        as_of=as_of,
        check_database=False,
        require_taker_buy=True,
    )
    if before.errors:
        return before
    for gap in before.gaps:
        sync_range(
            store,
            client,
            config.symbol,
            from_ms(gap.start_ms),
            from_ms(gap.end_ms),
            as_of=as_of,
            check_database=False,
            require_taker_buy=True,
        )
    return audit(
        store,
        config.symbol,
        from_ms(cursor),
        from_ms(end_ms),
        as_of=as_of,
        check_database=False,
        require_taker_buy=True,
    )


def run_collection(
    config: CollectorConfig,
    action: str,
    *,
    as_of: datetime | None = None,
    end: datetime | None = None,
    client: PublicDataClient | None = None,
    backup_path: Path | None = None,
) -> dict[str, object]:
    """Un lock durante la invocación completa; cada lote de mercado sigue siendo atómico."""
    if action not in {"history", "update", "audit", "backup"}:
        raise ValueError("Acción de colector desconocida")
    reference = require_utc(as_of or datetime.now(UTC))
    target = eligible_end(config, reference)
    through = end or target
    if minute_ms(through) > minute_ms(target) or through <= LAUNCH:
        raise ValueError("Fin vacío/futuro o anterior al retraso de publicación")
    if action != "history" and through != target:
        raise ValueError("El fin configurable corresponde únicamente a history-run")
    if action != "history" and not config.db_path.is_file():
        raise FileNotFoundError("No existe el histórico; no se crea una base vacía para actualizar")
    require_space(config.db_path, config.min_free_bytes)
    if action == "history":
        require_space(Path(tempfile.gettempdir()), config.min_free_bytes)
    public_client = _SpaceCheckedClient(
        client or PublicHTTP(request_interval_seconds=config.request_interval_seconds), config
    )
    result: dict[str, object] = {
        "action": action,
        "market": MARKET,
        "symbol": config.symbol.value,
        "as_of": reference.isoformat(),
        "eligible_end": target.isoformat(),
        "db_path": str(config.db_path.resolve()),
    }
    with FuturesSQLite(config.db_path) as store:
        begin_attempt(store, config.symbol, minute_ms(LAUNCH), action, datetime.now(UTC))
        _event("collector_started", **result)
        try:
            if action == "backup":
                if backup_path is None:
                    raise ValueError("Se requiere backup_path")
                _check_integrity(store)
                backed_up = consistent_backup(store, backup_path, reserve=config.min_free_bytes)
                progress = load_progress(store, config.symbol)
                assert progress is not None
                save_progress(store, config.symbol, replace(progress, last_state="complete"))
                result.update({"backup_path": str(backed_up), "complete": True})
            else:
                report: AuditReport | None
                if action == "history":
                    report = _history(store, public_client, config, through, reference)
                elif action == "audit":
                    report = audit(
                        store,
                        config.symbol,
                        LAUNCH,
                        target,
                        as_of=reference,
                        require_taker_buy=True,
                    )
                else:
                    report = _update(store, public_client, config, target, reference)
                if report is not None:
                    progress = record_audit(
                        store,
                        config.symbol,
                        report,
                        at=datetime.now(UTC),
                        eligible_end_ms=minute_ms(target),
                        full=action != "update",
                    )
                    result["audit"] = {**asdict(report), "range_complete": report.complete}
                    if report.errors:
                        raise ValueError("; ".join(report.errors))
                    result["complete"] = progress.ready and progress.audit_complete
                else:
                    result["complete"] = True
            clear_job_error(store, config.symbol, action)
            progress = load_progress(store, config.symbol)
            result["progress"] = None if progress is None else asdict(progress)
            result["gaps"] = [asdict(g) for g in pending_gaps(store, config.symbol)]
            if progress is not None:
                result["unverified_minutes"] = max(
                    0,
                    (minute_ms(target) - (progress.audit_end_ms or progress.start_ms)) // MINUTE_MS,
                )
            _event("collector_finished", **result)
        except BaseException as exc:
            logger.error(
                json.dumps(
                    {
                        "event": "collector_failed",
                        "action": action,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            )
            try:
                finish_error(
                    store,
                    config.symbol,
                    exc,
                    invalidate=isinstance(exc, (ValueError, sqlite3.DatabaseError)),
                )
            except Exception as persistence_error:
                logger.error(
                    json.dumps(
                        {
                            "event": "collector_error_record_failed",
                            "action": action,
                            "error_type": type(persistence_error).__name__,
                            "error": str(persistence_error),
                        }
                    )
                )
            raise
    return result


def collector_status(
    config: CollectorConfig, *, as_of: datetime | None = None
) -> dict[str, object]:
    """Estado read-only: no audita el histórico ni hace peticiones HTTP."""
    reference = require_utc(as_of or datetime.now(UTC))
    target = minute_ms(eligible_end(config, reference))
    with FuturesSQLite(config.db_path, read_only=True) as store:
        store.connection.execute("BEGIN")  # Un snapshot coherente de filas, progreso y errores.
        first, last, count = store.bounds(config.symbol)
        progress = load_progress(store, config.symbol)
        gaps = () if progress is None else pending_gaps(store, config.symbol)
        cursor = store.connection.execute(
            "SELECT * FROM import_manifest ORDER BY processed_at_utc DESC LIMIT 1"
        )
        row = cursor.fetchone()
        last_import = (
            None if row is None else dict(zip((d[0] for d in cursor.description), row, strict=True))
        )
        errors = list(store.conflicts(config.symbol)) + list(job_errors(store, config.symbol))
        if progress is not None and progress.last_error:
            errors.append(progress.last_error)
        checked = (
            minute_ms(LAUNCH) if progress is None else progress.audit_end_ms or progress.start_ms
        )
        missing = sum(max(0, min(g.end_ms, target) - g.start_ms) // MINUTE_MS for g in gaps)
        result: dict[str, object] = {
            "market": MARKET,
            "symbol": config.symbol.value,
            "db_path": str(store.path),
            "first_open_ms": first,
            "last_open_ms": last,
            "rows": count,
            "first_open_utc": None if first is None else from_ms(first).isoformat(),
            "last_open_utc": None if last is None else from_ms(last).isoformat(),
            "eligible_end_utc": from_ms(target).isoformat(),
            "lag_minutes": (target - (minute_ms(LAUNCH) if last is None else last + MINUTE_MS))
            // MINUTE_MS,
            "last_attempt_utc": None if progress is None else progress.last_attempt_utc,
            "last_successful_sync_utc": None if progress is None else progress.last_success_utc,
            "minutes_missing_verified": missing,
            "unverified_minutes": max(0, target - checked) // MINUTE_MS,
            "gaps": [asdict(g) for g in gaps],
            "pending_errors": errors,
            "last_import": last_import,
            "progress": None if progress is None else asdict(progress),
            "complete": progress is not None
            and progress.ready
            and not gaps
            and not errors
            and progress.validated_end_ms == target,
            "free_bytes": free_bytes(config.db_path),
            "required_reserve_bytes": config.min_free_bytes,
        }
        store.connection.execute("COMMIT")
        return result
