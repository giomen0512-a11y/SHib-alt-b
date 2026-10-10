"""Índice secundario reconstruible, anclado sólo a memoria de una sesión validada."""

import csv
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any

from trading_engine.binance.klines import CSV_HEADER
from trading_engine.models._validation import nonblank
from trading_engine.replay.encoding import encoded
from trading_engine.utils.time import require_utc

from .contracts import DEFAULT_LIMITS, DatasetType
from .local_files import file_digest, json_value, local_rows
from .parsers import parse_record, record_times
from .registry import digest, economic_identity, json_text, verify_dataset
from .segments import Registration, SegmentBounds
from .storage import connection
from .verification import SessionPolicy, VerificationCheckpoint, VerificationMode, VerificationState

CACHE_SCHEMA = """
CREATE TABLE cache_meta(version INTEGER PRIMARY KEY) STRICT;
INSERT INTO cache_meta VALUES(1);
CREATE TABLE locators(
 dataset TEXT NOT NULL, sequence INTEGER NOT NULL, effective TEXT NOT NULL,
 available TEXT NOT NULL, valid_until TEXT NOT NULL, event_id TEXT NOT NULL,
 raw_digest TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL,
 byte_digest TEXT NOT NULL, payload TEXT, PRIMARY KEY(dataset,sequence)
) STRICT;
CREATE INDEX time_locators ON locators(dataset,effective,available);
CREATE TABLE links(segment TEXT NOT NULL,dataset TEXT NOT NULL,sequence INTEGER NOT NULL,
 PRIMARY KEY(segment,sequence),FOREIGN KEY(dataset,sequence) REFERENCES locators(dataset,sequence)
) STRICT;
CREATE TABLE segments(id TEXT PRIMARY KEY,dataset TEXT NOT NULL,start TEXT NOT NULL,
 end TEXT NOT NULL,row_count INTEGER NOT NULL,digest TEXT NOT NULL) STRICT;
CREATE INDEX temporal_segments ON segments(dataset,start,end);
"""


def source_state(path: Path) -> tuple[int, int, int, int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextmanager
def read_cache(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, DEFAULT_LIMITS.max_json_bytes)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA cache_size=-1024")
        db.execute("BEGIN")
        if db.execute("SELECT version FROM cache_meta").fetchall() != [(1,)]:
            raise ValueError("CACHE_SCHEMA_CHANGED")
        yield db
    finally:
        db.close()


def physical_rows(spec: Registration) -> Iterator[tuple[int, object, int, int, str, str | None]]:
    path = Path(spec.original_file)
    if path.suffix.lower() == ".zip" or spec.file_format == "json":
        # Un snapshot derivado acotado en disco; no promete acceso aleatorio al ZIP.
        for sequence, row in enumerate(
            local_rows(path, spec.limits, file_format=spec.file_format, member=spec.member), 1
        ):
            payload = json_text(row)
            yield sequence, row, -1, len(payload.encode()), digest(payload), payload
        return
    total = 0
    with path.open("rb") as stream:
        first = True

        def lines() -> Iterator[str]:
            nonlocal total, first
            while line := stream.readline(spec.limits.max_line_bytes + 1):
                total += len(line)
                if (
                    len(line) > spec.limits.max_line_bytes
                    or total > spec.limits.max_uncompressed_bytes
                ):
                    raise ValueError("PHYSICAL_LINE_LIMIT")
                text = line.decode("utf-8-sig" if first else "utf-8")
                first = False
                yield text

        if spec.file_format == "csv":
            rows: Iterator[object] = iter(csv.reader(lines()))
        else:
            rows = (json_value(line) for line in lines() if line.strip())
        offset = 0
        for sequence, row in enumerate(rows, 1):
            if sequence > spec.limits.max_rows:
                raise ValueError("PHYSICAL_ROW_LIMIT")
            end = stream.tell()
            if end - offset > spec.limits.max_line_bytes * 12:
                raise ValueError("PHYSICAL_RECORD_LIMIT")
            # CSV puede tener un registro multilínea. El locator es una frontera real.
            saved = stream.tell()
            stream.seek(offset)
            data = stream.read(end - offset)
            stream.seek(saved)
            yield sequence, row, offset, end - offset, sha256(data).hexdigest(), None
            offset = end


@dataclass(frozen=True)
class SegmentRoot:
    identity: str
    dataset_id: str
    bounds: SegmentBounds
    row_count: int
    content_digest: str
    registry_seal: tuple[object, ...]


class SealedSegmentSession:
    """Confianza de proceso explícita, TTL monotónico y raíces no heredables en disco."""

    def __init__(
        self,
        path: Path,
        dataset_ids: tuple[str, ...],
        *,
        session_identity: str,
        opened_at: datetime,
        policy: SessionPolicy,
    ) -> None:
        from .query import HistoricalQuery

        require_utc(opened_at)
        nonblank(session_identity, "session_identity")
        if not isinstance(policy, SessionPolicy):
            raise TypeError("SessionPolicy explícita requerida")
        self.query = HistoricalQuery(path, dataset_ids)
        self.identity, self.opened_at, self.policy = session_identity, opened_at, policy
        self._started = monotonic()
        self._queries = 0
        self._closed = False
        self._invalid = False
        self.specs: dict[str, Registration] = {}
        self.source_hashes: dict[str, str] = {}
        self.audit_ids: dict[str, str] = {}
        self.states: dict[str, tuple[int, int, int, int, int]] = {}
        self.roots: tuple[SegmentRoot, ...] = ()
        self.issued_checkpoints: dict[str, object] = {}
        self.verified_checkpoints: dict[str, VerificationCheckpoint] = {}
        self._temporary = tempfile.TemporaryDirectory(prefix="shib-sealed-session-")
        self.cache_path = Path(self._temporary.name) / "locators.sqlite3"
        self.metrics: dict[str, float] = {}
        started = perf_counter()
        try:
            self._prepare()
            self.root_id = digest(
                (self.query.selection_id, self.roots, tuple(sorted(self.source_hashes.items())))
            )
            self.metrics["session_open_seconds"] = perf_counter() - started
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "SealedSegmentSession":
        self.check()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._temporary.cleanup()
            self.issued_checkpoints.clear()
            self.verified_checkpoints.clear()

    def _prepare(self) -> None:
        cache = sqlite3.connect(self.cache_path)
        cache.execute("PRAGMA foreign_keys=ON")
        cache.execute("PRAGMA cache_size=-1024")
        cache.executescript(CACHE_SCHEMA)
        roots: list[SegmentRoot] = []
        try:
            with connection(self.query.path) as db:
                validation = perf_counter()
                for identity in self.query.dataset_ids:
                    # Capturar estado ANTES de verificar y comparar DESPUÉS de indexar.
                    row = db.execute("SELECT spec FROM datasets WHERE id=?", (identity,)).fetchone()
                    if row is None:
                        raise ValueError("DATASET_NOT_PROVIDED")
                    from .registry import decode_registration

                    original = Path(decode_registration(row[0]).original_file)
                    self.states[identity] = source_state(original)
                    self.specs[identity] = verify_dataset(db, identity)
                    saved = db.execute(
                        "SELECT source_hash,audit_id FROM datasets WHERE id=?", (identity,)
                    ).fetchone()
                    self.source_hashes[identity], self.audit_ids[identity] = saved
                self.metrics["full_validation_seconds"] = perf_counter() - validation
                indexing = perf_counter()
                for identity, spec in self.specs.items():
                    cursor = iter(
                        db.execute(
                            "SELECT sequence,effective,available,valid_until,event_id,"
                            "raw_digest,payload "
                            "FROM records WHERE dataset_id=? ORDER BY sequence",
                            (identity,),
                        )
                    )
                    for sequence, raw, offset, length, byte_hash, payload in physical_rows(spec):
                        if sequence == 1 and isinstance(raw, list) and tuple(raw) == CSV_HEADER:
                            continue
                        saved = next(cursor, None)
                        record = parse_record(
                            raw, spec.declaration.dataset_type, as_of=spec.validation_time
                        )
                        if (
                            saved is None
                            or saved[0] != sequence
                            or (
                                saved[5] != sha256(json_text(raw).encode()).hexdigest()
                                or saved[4]
                                != economic_identity(raw, spec.declaration.dataset_type, record)
                                or saved[1:3]
                                != tuple(
                                    t.isoformat()
                                    for t in record_times(raw, spec.declaration.dataset_type)
                                )
                            )
                        ):
                            raise ValueError("SOURCE_CHANGED_DURING_INDEX")
                        cache.execute(
                            "INSERT INTO locators VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (identity, *saved[:6], offset, length, byte_hash, payload),
                        )
                    if next(cursor, None) is not None:
                        raise ValueError("MISSING_PHYSICAL_RECORD")
                    if (
                        file_digest(Path(spec.original_file), spec.limits)
                        != self.source_hashes[identity]
                    ):
                        raise ValueError("SOURCE_CHANGED")
                    for bounds in spec.segments:
                        segment_id = digest((identity, bounds))
                        cache.execute(
                            "INSERT INTO links SELECT ?,dataset,sequence FROM locators "
                            "WHERE dataset=? AND effective<? AND valid_until>?",
                            (
                                segment_id,
                                identity,
                                bounds.end.isoformat(),
                                bounds.start.isoformat(),
                            ),
                        )
                        fingerprint, count = sha256(), 0
                        for row in cache.execute(
                            "SELECT l.* FROM locators l JOIN links k ON "
                            "l.dataset=k.dataset AND l.sequence=k.sequence "
                            "WHERE k.segment=? ORDER BY l.sequence",
                            (segment_id,),
                        ):
                            fingerprint.update(encoded(tuple(row)))
                            count += 1
                        registry_seal = db.execute(
                            "SELECT * FROM segments WHERE id=?", (segment_id,)
                        ).fetchone()
                        cache.execute(
                            "INSERT INTO segments VALUES(?,?,?,?,?,?)",
                            (
                                segment_id,
                                identity,
                                bounds.start.isoformat(),
                                bounds.end.isoformat(),
                                count,
                                fingerprint.hexdigest(),
                            ),
                        )
                        roots.append(
                            SegmentRoot(
                                segment_id,
                                identity,
                                bounds,
                                count,
                                fingerprint.hexdigest(),
                                tuple(registry_seal),
                            )
                        )
                        if len(roots) > 1000:
                            raise ValueError("SESSION_SEGMENT_LIMIT")
                    if source_state(Path(spec.original_file)) != self.states[identity]:
                        raise ValueError("SOURCE_CHANGED")
                self.metrics["index_seconds"] = perf_counter() - indexing
            cache.commit()
            if cache.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("CACHE_INTEGRITY_FAILED")
            if cache.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("CACHE_REFERENCE_FAILED")
            self.roots = tuple(roots)
        finally:
            cache.close()

    def check(self, *, new_query: bool = False) -> None:
        if self._closed or self._invalid:
            raise ValueError("VERIFICATION_NOT_AVAILABLE")
        if monotonic() - self._started > self.policy.max_seconds:
            self._invalid = True
            raise ValueError("SESSION_EXPIRED")
        if new_query:
            if self._queries >= self.policy.max_queries:
                raise ValueError("SESSION_QUERY_LIMIT")
            self._queries += 1
        for identity, spec in self.specs.items():
            try:
                changed = source_state(Path(spec.original_file)) != self.states[identity]
            except OSError:
                changed = True
            if changed:
                self._invalid = True
                raise ValueError("SOURCE_CHANGED")

    def selected_roots(
        self, kind: DatasetType, start: datetime, end: datetime
    ) -> tuple[SegmentRoot, ...]:
        self.check()
        expected = tuple(
            r
            for r in self.roots
            if self.specs[r.dataset_id].declaration.dataset_type is kind
            and r.bounds.start < end
            and r.bounds.end > start
        )
        found: list[tuple[object, ...]] = []
        with read_cache(self.cache_path) as db:
            for identity, spec in self.specs.items():
                if spec.declaration.dataset_type is kind:
                    found.extend(
                        db.execute(
                            "SELECT * FROM segments WHERE dataset=? AND start<? AND end>?",
                            (identity, end.isoformat(), start.isoformat()),
                        )
                    )
        trusted = [
            (
                r.identity,
                r.dataset_id,
                r.bounds.start.isoformat(),
                r.bounds.end.isoformat(),
                r.row_count,
                r.content_digest,
            )
            for r in expected
        ]
        if sorted(found) != sorted(trusted):
            self._invalid = True
            raise ValueError("SEGMENT_SELECTION_INDEX_CHANGED")
        return expected

    def verify_segments(
        self, roots: tuple[SegmentRoot, ...], destination: sqlite3.Connection
    ) -> VerificationCheckpoint:
        self.check(new_query=True)
        inserted = 0
        try:
            with read_cache(self.cache_path) as db:
                for root in roots:
                    spec = self.specs[root.dataset_id]
                    fingerprint, count = sha256(), 0
                    path = Path(spec.original_file)
                    with path.open("rb") as original:
                        fd = os.fstat(original.fileno())
                        if (fd.st_dev, fd.st_ino) != self.states[root.dataset_id][:2]:
                            raise ValueError("SOURCE_CHANGED")
                        for row in db.execute(
                            "SELECT l.* FROM locators l JOIN links k ON "
                            "l.dataset=k.dataset AND l.sequence=k.sequence "
                            "WHERE k.segment=? ORDER BY l.sequence",
                            (root.identity,),
                        ):
                            fingerprint.update(encoded(tuple(row)))
                            count += 1
                            raw = self._raw(row, spec, original)
                            record = parse_record(
                                raw, spec.declaration.dataset_type, as_of=spec.validation_time
                            )
                            if (
                                sha256(json_text(raw).encode()).hexdigest() != row[6]
                                or economic_identity(raw, spec.declaration.dataset_type, record)
                                != row[5]
                                or tuple(
                                    t.isoformat()
                                    for t in record_times(raw, spec.declaration.dataset_type)
                                )
                                != row[2:4]
                            ):
                                raise ValueError("SEGMENT_RECORD_CHANGED")
                            # Disco secundario: snapshot del intervalo, no una fuente oficial Trade.
                            insertion = destination.execute(
                                "INSERT OR IGNORE INTO inputs VALUES(?,?,?,?,?,?,?,?,?)",
                                (*row[:7], json_text(raw), root.identity),
                            )
                            inserted += insertion.rowcount
                            if inserted > DEFAULT_LIMITS.max_rows:
                                raise ValueError("QUERY_INPUT_ROW_LIMIT")
                    if count != root.row_count or fingerprint.hexdigest() != root.content_digest:
                        raise ValueError("SEGMENT_INDEX_CHANGED")
            self.check()
            checkpoint = VerificationCheckpoint(
                self.query.selection_id,
                self.query.dataset_ids,
                tuple(self.audit_ids[i] for i in self.query.dataset_ids),
                tuple(self.source_hashes[i] for i in self.query.dataset_ids),
                tuple(r.identity for r in roots),
                tuple(r.content_digest for r in roots),
                tuple((r.bounds.start, r.bounds.end) for r in roots),
                VerificationMode.SEALED_SEGMENT_SESSION,
                "SELECTED_SEGMENTS_AND_STABLE_SESSION_SNAPSHOT",
                self.identity,
                self.root_id,
                VerificationState.SEGMENT_VERIFIED,
                VerificationState.SOURCE_NOT_REVALIDATED,
            )
            self.verified_checkpoints[digest(checkpoint)] = checkpoint
            return checkpoint
        except BaseException:
            self._invalid = True
            raise

    @staticmethod
    def _raw(row: tuple[Any, ...], spec: Registration, original: Any) -> object:
        offset, length, byte_hash, payload = row[7:11]
        if payload is not None:
            if digest(payload) != byte_hash:
                raise ValueError("DERIVED_PAYLOAD_CHANGED")
            return json_value(payload)
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or not 0 < length <= spec.limits.max_line_bytes * 12
        ):
            raise ValueError("INVALID_PHYSICAL_LOCATOR")
        original.seek(offset)
        data = original.read(length)
        if len(data) != length or sha256(data).hexdigest() != byte_hash:
            raise ValueError("SEGMENT_BYTES_CHANGED")
        text = data.decode("utf-8-sig" if offset == 0 else "utf-8")
        if spec.file_format == "csv":
            rows = list(csv.reader(text.splitlines(keepends=True)))
            if len(rows) != 1:
                raise ValueError("LOCATOR_RECORD_BOUNDARY_CHANGED")
            return rows[0]
        # El locator JSONL incluye blancos precedentes; exactamente un registro no vacío.
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) != 1:
            raise ValueError("LOCATOR_RECORD_BOUNDARY_CHANGED")
        return json_value(lines[0])
