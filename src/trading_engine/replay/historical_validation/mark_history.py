"""Snapshot Mark causal verificado, acotado en disco y sin autorización financiera."""

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from trading_engine.binance.klines import LAUNCH, minute_ms
from trading_engine.replay.encoding import encoded
from trading_engine.utils.time import require_utc

from .contracts import DEFAULT_LIMITS, Contract, DatasetType, exact_decimal, utc_text
from .fast_query import QueryStream, historical_query
from .parsers import MarkKline
from .query import BlockedHistoricalInterval, HistoricalEvent
from .reconciliation import HistoricalReconciliation, ReconciliationReport
from .registry import decode_registration, digest
from .replay_adapter import DEFAULT_ADAPTER_LIMITS, AdapterLimits, PreparedBlock
from .segment_session import SealedSegmentSession
from .segments import Registration
from .storage import connection
from .verification import VerificationCheckpoint, VerificationMode, VerificationState

MINUTE = timedelta(minutes=1)
BLOCK_ROWS = 1440


@dataclass(frozen=True)
class MarkSourceBinding:
    dataset_id: str
    audit_id: str
    source_digest: str
    registration: Registration


@dataclass(frozen=True)
class PreparedMarkEvidence:
    start: datetime
    end: datetime
    cutoff: datetime
    mode: VerificationMode
    bindings: tuple[MarkSourceBinding, ...]
    reconciliation: ReconciliationReport
    verification: VerificationCheckpoint | None
    source_state: VerificationState
    original_state: VerificationState
    identity: str
    economic_identity: str
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"


@dataclass(frozen=True)
class PreparedMarkRow:
    sequence: int
    event: HistoricalEvent
    continuous: bool


def bindings(
    path: Path, ids: tuple[str, ...], session: SealedSegmentSession | None = None
) -> tuple[MarkSourceBinding, ...]:
    if session is not None:
        session.check()
        frozen = tuple(
            MarkSourceBinding(i, session.audit_ids[i], session.source_hashes[i], session.specs[i])
            for i in ids
        )
        if any(b.registration.declaration.dataset_type is not DatasetType.MARK for b in frozen):
            raise ValueError("MARK_DATASETS_REQUIRED")
        return frozen
    result: list[MarkSourceBinding] = []
    with connection(path) as db:
        for identity in ids:
            row = db.execute(
                "SELECT spec,audit_id,source_hash,state FROM datasets WHERE id=?", (identity,)
            ).fetchone()
            if row is None or row[3] != "COMPLETE_IMPORT":
                raise ValueError("MARK_DATASET_NOT_COMPLETE")
            spec = decode_registration(row[0])
            if (
                spec.declaration.dataset_type is not DatasetType.MARK
                or spec.declaration.contract != Contract()
                or spec.declaration.interval != "1m"
            ):
                raise ValueError("MARK_DATASETS_REQUIRED")
            result.append(MarkSourceBinding(identity, row[1], row[2], spec))
    return tuple(result)


def decode(text: str) -> HistoricalEvent:
    data: dict[str, Any] = json.loads(text)
    raw = data["record"]
    record = MarkKline(
        utc_text(raw["market_effective_at"]),
        utc_text(raw["market_available_at"]),
        exact_decimal(raw["open"], "open"),
        exact_decimal(raw["high"], "high"),
        exact_decimal(raw["low"], "low"),
        exact_decimal(raw["close"], "close"),
        raw["quality"],
        raw["intraminute_path"],
    )
    return HistoricalEvent(
        data["event_id"],
        utc_text(data["market_effective_at"]),
        utc_text(data["market_available_at"]),
        utc_text(data["valid_until"]),
        record,
        tuple(data["dataset_ids"]),
        tuple(data["sources"]),
        tuple(data["provenance"]),
        data["duplicates"],
        data["authentication"],
        data["economic_authorization"],
    )


class PreparedMarkHistory:
    def __init__(
        self,
        path: Path,
        dataset_ids: tuple[str, ...],
        start: datetime,
        end: datetime,
        *,
        cutoff: datetime,
        mode: VerificationMode = VerificationMode.STRICT_FULL,
        session: SealedSegmentSession | None = None,
        limits: AdapterLimits = DEFAULT_ADAPTER_LIMITS,
    ) -> None:
        minute_ms(start)
        minute_ms(end)
        require_utc(cutoff)
        if start < LAUNCH or start >= end or cutoff < start + MINUTE:
            raise ValueError("NO_VERIFIED_MARK_PREFIX")
        if not isinstance(limits, AdapterLimits):
            raise TypeError("AdapterLimits requerido")
        if (min(end, cutoff) - start) // MINUTE > limits.max_rows:
            raise ValueError("MARK_ROW_LIMIT")
        if type(dataset_ids) is not tuple or not dataset_ids:
            raise ValueError("EXPLICIT_MARK_DATASET_IDS_REQUIRED")
        query = historical_query(path, dataset_ids, mode=mode, session=session)
        selected = bindings(query.path, query.dataset_ids, session)
        self.start, self.end, self.cutoff = start, end, cutoff
        self._source_session = session
        self._token = object()
        self._ready = False
        self._db: sqlite3.Connection | None = None
        self._temporary = tempfile.TemporaryDirectory(prefix="shib-prepared-mark-")
        self.path = Path(self._temporary.name) / "mark.sqlite3"
        self.blocks: tuple[PreparedBlock, ...] = ()
        self._cached: PreparedBlock | None = None
        self._rows: tuple[PreparedMarkRow, ...] = ()
        self.metrics: dict[str, float | int] = {}
        begun = perf_counter()
        try:
            reconciliation = HistoricalReconciliation(query).evaluate(
                DatasetType.MARK, start, end, cutoff=cutoff
            )
            self.metrics["validation_seconds"] = perf_counter() - begun
            if reconciliation.conflicts:
                raise BlockedHistoricalInterval(reconciliation.conflicts[0])
            stream = query.read_mark(start, reconciliation.end, cutoff=cutoff)
            verification = stream.verification if isinstance(stream, QueryStream) else None
            try:
                count, duplicates, fingerprint = self._stage(stream, limits)
                if isinstance(stream, QueryStream) and (
                    stream.state != "COMPLETE" or stream.causal_digest != fingerprint
                ):
                    raise ValueError("MARK_QUERY_NOT_COMPLETE")
            finally:
                if hasattr(stream, "close"):
                    stream.close()
            if (
                count != reconciliation.recognized
                or duplicates != reconciliation.duplicates
                or fingerprint != reconciliation.causal_digest
                or selected != bindings(query.path, query.dataset_ids, session)
            ):
                raise ValueError("MARK_RECONCILIATION_CHANGED")
            self.row_count = count
            identity = digest(("prepared-mark-v1", start, end, cutoff, mode, selected, self.blocks))
            self.evidence = PreparedMarkEvidence(
                start,
                end,
                cutoff,
                mode,
                selected,
                reconciliation,
                verification,
                VerificationState.FULL_SOURCE_VERIFIED
                if verification is None
                else verification.source_state,
                VerificationState.FULL_SOURCE_VERIFIED
                if verification is None
                else verification.original_state,
                identity,
                digest(("mark-inputs-v1", start, end, cutoff, selected, fingerprint)),
            )
            self._open_snapshot()
            self._ready = True
            self.metrics.update(
                rows=count,
                blocks=len(self.blocks),
                temporary_bytes=self.path.stat().st_size,
                preparation_seconds=perf_counter() - begun,
            )
        except BaseException:
            self.close()
            raise

    def _stage(
        self, stream: Iterator[HistoricalEvent], limits: AdapterLimits
    ) -> tuple[int, int, str]:
        roots: list[PreparedBlock] = []
        count = duplicates = 0
        fingerprint, block_hash = sha256(), sha256()
        previous = self.start
        block_start = self.start
        with closing(sqlite3.connect(self.path)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, DEFAULT_LIMITS.max_json_bytes)
            db.execute("PRAGMA page_size=4096")
            db.execute(f"PRAGMA max_page_count={limits.max_temporary_bytes // 4096}")
            db.execute("PRAGMA cache_size=-1024")
            db.execute(
                "CREATE TABLE marks(sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, "
                "continuous INTEGER NOT NULL) STRICT"
            )
            for count, event in enumerate(stream, 1):
                record = event.record
                if (
                    not isinstance(record, MarkKline)
                    or event.market_effective_at < previous
                    or event.market_effective_at < self.start
                    or event.market_available_at > min(self.end, self.cutoff)
                    or event.market_available_at != event.market_effective_at + MINUTE
                    or record.market_effective_at != event.market_effective_at
                    or record.market_available_at != event.market_available_at
                    or event.valid_until != event.market_available_at
                    or record.close <= 0
                    or record.quality != "CLOSED_MARK_OHLC_1M"
                    or record.intraminute_path != "UNKNOWN_INTRAMINUTE_PATH"
                    or event.authentication != "NOT_AUTHENTICATED"
                    or event.economic_authorization != "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
                ):
                    raise ValueError("INVALID_MARK_EVENT")
                if count > limits.max_rows:
                    raise ValueError("MARK_ROW_LIMIT")
                minute_ms(event.market_effective_at)
                if (count - 1) % BLOCK_ROWS == 0:
                    block_start = event.market_effective_at
                row = (count, encoded(event).decode(), int(event.market_effective_at == previous))
                db.execute("INSERT INTO marks VALUES(?,?,?)", row)
                block_hash.update(encoded(row))
                fingerprint.update(encoded(event.economic_key()))
                duplicates += event.duplicates
                previous = event.market_available_at
                if count % BLOCK_ROWS == 0:
                    roots.append(
                        PreparedBlock(
                            count - BLOCK_ROWS + 1,
                            count,
                            block_start,
                            previous,
                            block_hash.hexdigest(),
                        )
                    )
                    block_hash = sha256()
            if count % BLOCK_ROWS:
                roots.append(
                    PreparedBlock(
                        len(roots) * BLOCK_ROWS + 1,
                        count,
                        block_start,
                        previous,
                        block_hash.hexdigest(),
                    )
                )
            db.commit()
        if self.path.stat().st_size > limits.max_temporary_bytes:
            raise ValueError("MARK_STORAGE_LIMIT")
        self.blocks = tuple(roots)
        return count, duplicates, fingerprint.hexdigest()

    def _open_snapshot(self) -> None:
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        self._db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, DEFAULT_LIMITS.max_json_bytes)
        self._db.execute("PRAGMA query_only=ON")
        self._db.execute("PRAGMA cache_size=-1024")
        if self._db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("MARK_SNAPSHOT_CORRUPT")
        self._db.execute("BEGIN")
        self._db.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()

    def check(self) -> None:
        if not self._ready:
            raise ValueError("PREPARED_MARK_NOT_AVAILABLE")
        if self._source_session is not None:
            self._source_session.check()

    @property
    def session(self) -> object:
        self.check()
        return self._token

    def _block(self, root: PreparedBlock) -> tuple[PreparedMarkRow, ...]:
        self.check()
        if self._cached == root:
            return self._rows
        assert self._db is not None
        fingerprint = sha256()
        values: list[PreparedMarkRow] = []
        try:
            rows = tuple(
                self._db.execute(
                    "SELECT sequence,payload,continuous FROM marks "
                    "WHERE sequence BETWEEN ? AND ? ORDER BY sequence",
                    (root.first_sequence, root.last_sequence),
                )
            )
            for index, row in enumerate(rows):
                if row[0] != root.first_sequence + index:
                    raise ValueError("MARK_CURSOR_CHANGED")
                fingerprint.update(encoded(row))
            if (
                len(rows) != root.last_sequence - root.first_sequence + 1
                or fingerprint.hexdigest() != root.content_digest
            ):
                raise ValueError("MARK_BLOCK_CHANGED")
            # El payload solo se interpreta después de acreditar el bloque completo.
            values = [PreparedMarkRow(row[0], decode(row[1]), bool(row[2])) for row in rows]
        except BaseException:
            self.close()
            raise
        self._cached, self._rows = root, tuple(values)
        return self._rows

    def read(self, cursor: int, until: datetime) -> Iterator[PreparedMarkRow]:
        self.check()
        require_utc(until)
        if type(cursor) is not int or not 0 <= cursor <= self.row_count:
            raise ValueError("MARK_CURSOR_INVALID")
        if not self.start <= until <= self.end:
            raise ValueError("MARK_OUTSIDE_REQUEST")
        if until > self.cutoff:
            raise ValueError("MARK_BEYOND_CUTOFF")
        for root in self.blocks:
            if root.last_sequence <= cursor:
                continue
            if root.start + MINUTE > until:
                break
            for row in self._block(root):
                self.check()
                if row.sequence > cursor and row.event.market_available_at <= until:
                    yield row
        self.check()

    def close(self) -> None:
        self._ready = False
        self._rows = ()
        self._cached = None
        if self._db is not None:
            self._db.close()
            self._db = None
        self._temporary.cleanup()

    def __enter__(self) -> "PreparedMarkHistory":
        self.check()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
