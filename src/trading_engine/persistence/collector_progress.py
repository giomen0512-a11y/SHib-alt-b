"""Checkpoints operativos atómicos; no contienen estado de estrategia."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast

from trading_engine.binance.klines import MARKET, MINUTE_MS, from_ms
from trading_engine.market_data.audit import AuditReport, Gap
from trading_engine.models.values import Symbol
from trading_engine.persistence.futures_sqlite import FuturesSQLite
from trading_engine.utils.time import require_utc


@dataclass(frozen=True)
class CollectorProgress:
    start_ms: int
    validated_end_ms: int
    ready: bool = False
    last_attempt_utc: str | None = None
    last_success_utc: str | None = None
    last_action: str | None = None
    last_state: str = "new"
    last_error: str | None = None
    audit_at_utc: str | None = None
    audit_complete: bool = False
    audit_end_ms: int | None = None


def load_progress(store: FuturesSQLite, symbol: Symbol) -> CollectorProgress | None:
    if (
        store.connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='collector_state'"
        ).fetchone()
        is None
    ):
        return None  # Base Fase Datos 1 aún no migrada; status sigue siendo read-only.
    row = store.connection.execute(
        "SELECT start_ms, validated_end_ms, ready, last_attempt_utc, last_success_utc, "
        "last_action, last_state, last_error, audit_at_utc, audit_complete, audit_end_ms "
        "FROM collector_state WHERE market=? AND symbol=?",
        (MARKET, symbol.value),
    ).fetchone()
    if row is None:
        return None
    if row[0] > row[1] or row[0] % MINUTE_MS or row[1] % MINUTE_MS:
        raise ValueError("Checkpoint operativo corrupto")
    from_ms(row[0])
    from_ms(row[1])
    if row[2] not in (0, 1) or row[9] not in (0, 1):
        raise ValueError("Estado operativo corrupto")
    return CollectorProgress(
        row[0],
        row[1],
        bool(row[2]),
        row[3],
        row[4],
        row[5],
        row[6],
        row[7],
        row[8],
        bool(row[9]),
        row[10],
    )


def pending_gaps(store: FuturesSQLite, symbol: Symbol) -> tuple[Gap, ...]:
    return tuple(
        Gap(row[0], row[1])
        for row in store.connection.execute(
            "SELECT start_ms, end_ms FROM collector_gaps WHERE market=? AND symbol=? "
            "ORDER BY start_ms",
            (MARKET, symbol.value),
        )
    )


def save_progress(
    store: FuturesSQLite,
    symbol: Symbol,
    progress: CollectorProgress,
    gaps: tuple[Gap, ...] | None = None,
) -> None:
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        store.connection.execute(
            """INSERT INTO collector_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market, symbol) DO UPDATE SET
            start_ms=excluded.start_ms, validated_end_ms=excluded.validated_end_ms,
            ready=excluded.ready, last_attempt_utc=excluded.last_attempt_utc,
            last_success_utc=excluded.last_success_utc, last_action=excluded.last_action,
            last_state=excluded.last_state, last_error=excluded.last_error,
            audit_at_utc=excluded.audit_at_utc, audit_complete=excluded.audit_complete,
            audit_end_ms=excluded.audit_end_ms""",
            (
                MARKET,
                symbol.value,
                progress.start_ms,
                progress.validated_end_ms,
                int(progress.ready),
                progress.last_attempt_utc,
                progress.last_success_utc,
                progress.last_action,
                progress.last_state,
                progress.last_error,
                progress.audit_at_utc,
                int(progress.audit_complete),
                progress.audit_end_ms,
            ),
        )
        if gaps is not None:
            store.connection.execute(
                "DELETE FROM collector_gaps WHERE market=? AND symbol=?", (MARKET, symbol.value)
            )
            store.connection.executemany(
                "INSERT INTO collector_gaps VALUES (?, ?, ?, ?)",
                ((MARKET, symbol.value, g.start_ms, g.end_ms) for g in gaps),
            )
        store.connection.execute("COMMIT")
    except BaseException:
        store.connection.execute("ROLLBACK")
        raise


def begin_attempt(
    store: FuturesSQLite, symbol: Symbol, start_ms: int, action: str, at: datetime
) -> CollectorProgress:
    progress = load_progress(store, symbol) or CollectorProgress(start_ms, start_ms)
    if progress.start_ms != start_ms:
        raise ValueError("El inicio no coincide con el histórico configurado")
    progress = replace(
        progress,
        last_attempt_utc=require_utc(at).isoformat(),
        last_action=action,
        last_state="running",
        last_error=None,
    )
    save_progress(store, symbol, progress)
    return progress


def finish_error(
    store: FuturesSQLite, symbol: Symbol, error: BaseException, *, invalidate: bool = False
) -> None:
    progress = load_progress(store, symbol)
    if progress is not None:
        store.connection.execute(
            "INSERT INTO collector_errors VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(market, symbol, action) DO UPDATE SET error=excluded.error, "
            "at_utc=excluded.at_utc",
            (
                MARKET,
                symbol.value,
                progress.last_action or "unknown",
                str(error),
                progress.last_attempt_utc or "",
            ),
        )
        save_progress(
            store,
            symbol,
            replace(
                progress,
                last_state="failed",
                last_error=str(error),
                ready=progress.ready and not invalidate,
                audit_complete=False,
            ),
        )


def clear_job_error(store: FuturesSQLite, symbol: Symbol, action: str) -> None:
    store.connection.execute(
        "DELETE FROM collector_errors WHERE market=? AND symbol=? AND action=?",
        (MARKET, symbol.value, action),
    )


def job_errors(store: FuturesSQLite, symbol: Symbol) -> tuple[str, ...]:
    if (
        store.connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='collector_errors'"
        ).fetchone()
        is None
    ):
        return ()
    return tuple(
        f"{row[0]}: {row[1]}"
        for row in store.connection.execute(
            "SELECT action, error FROM collector_errors "
            "WHERE market=? AND symbol=? ORDER BY action",
            (MARKET, symbol.value),
        )
    )


def record_audit(
    store: FuturesSQLite,
    symbol: Symbol,
    report: AuditReport,
    *,
    at: datetime,
    eligible_end_ms: int,
    full: bool,
) -> CollectorProgress:
    progress = cast(CollectorProgress, load_progress(store, symbol))
    if progress is None:
        raise ValueError("Falta estado operativo")
    if full:
        gaps = report.gaps
        ready = not report.errors and (
            progress.ready or (report.complete and report.end_ms >= eligible_end_ms)
        )
    else:
        pieces = list(report.gaps)
        for gap in pending_gaps(store, symbol):
            if gap.start_ms < report.start_ms:
                pieces.append(Gap(gap.start_ms, min(gap.end_ms, report.start_ms)))
            if gap.end_ms > report.end_ms:
                pieces.append(Gap(max(gap.start_ms, report.end_ms), gap.end_ms))
        merged: list[Gap] = []
        for gap in sorted(pieces, key=lambda g: g.start_ms):
            if merged and gap.start_ms <= merged[-1].end_ms:
                merged[-1] = Gap(merged[-1].start_ms, max(merged[-1].end_ms, gap.end_ms))
            else:
                merged.append(gap)
        gaps = tuple(merged)
        ready = progress.ready and not report.errors
    cursor = report.end_ms if not gaps else min(report.end_ms, gaps[0].start_ms)
    complete = ready and not report.errors and not gaps and cursor >= eligible_end_ms
    progress = replace(
        progress,
        validated_end_ms=cursor,
        ready=ready,
        last_state="complete" if complete else "pending",
        last_error="; ".join(report.errors) or None,
        last_success_utc=at.isoformat()
        if complete and progress.last_action in {"history", "update"}
        else progress.last_success_utc,
        audit_at_utc=at.isoformat() if full else progress.audit_at_utc,
        audit_complete=complete,
        audit_end_ms=report.end_ms
        if full
        else max(progress.audit_end_ms or progress.start_ms, report.end_ms),
    )
    save_progress(store, symbol, progress, gaps)
    return progress
