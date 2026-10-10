"""Consultas opt-in con preflight en disco y finalización explícita del stream."""

import heapq
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from trading_engine.binance.klines import LAUNCH, minute_ms
from trading_engine.replay.encoding import encoded
from trading_engine.utils.time import require_utc

from .contracts import DEFAULT_LIMITS, DatasetType, utc_text
from .parsers import parse_record
from .query import (
    BlockedHistoricalInterval,
    HistoricalConflict,
    HistoricalEvent,
    HistoricalQuery,
    _combine,
    _rules_overlap,
)
from .registry import digest, economic_identity, json_text
from .rules import FeeRules, HistoricalBrackets, RuleQuality, SymbolRules
from .segment_session import SealedSegmentSession
from .verification import QueryRecoveryCheckpoint, VerificationMode


def _groups(
    streams: list[Iterator[HistoricalEvent]],
) -> Iterator[HistoricalEvent | HistoricalConflict]:
    pending: HistoricalEvent | None = None
    group: list[HistoricalEvent] = []

    def emit() -> HistoricalEvent | HistoricalConflict:
        nonlocal pending
        combined = _combine(group)
        if isinstance(combined, HistoricalConflict):
            widest = max(group, key=lambda e: e.valid_until)
            if pending is None or widest.valid_until > pending.valid_until:
                pending = widest
        elif pending is not None and _rules_overlap(pending, combined):
            conflict = HistoricalConflict(
                combined.market_effective_at,
                min(pending.valid_until, combined.valid_until),
                tuple(sorted(set(pending.dataset_ids + combined.dataset_ids))),
                (pending.event_id, combined.event_id),
                "CONFLICTED_RULE_VALIDITY",
            )
            if combined.valid_until > pending.valid_until:
                pending = combined
            return conflict
        else:
            pending = combined
        return combined

    for event in heapq.merge(*streams, key=lambda e: (e.market_effective_at, e.event_id)):
        if group and event.market_effective_at != group[0].market_effective_at:
            yield emit()
            group = []
        group.append(event)
        if len(group) > 10000:
            raise ValueError("CONFLICT_GROUP_LIMIT_EXCEEDED")
    if group:
        yield emit()


class QueryStream[T: HistoricalEvent | HistoricalConflict](Iterator[T]):
    """Los registros son PROVISIONAL hasta agotamiento exitoso; nunca commit económico."""

    def __init__(
        self,
        session: SealedSegmentSession,
        kind: DatasetType,
        start: datetime,
        end: datetime,
        cutoff: datetime,
        *,
        allow_conflicts: bool,
    ) -> None:
        for at in (start, end, cutoff):
            require_utc(at)
        if start < LAUNCH or start >= end:
            raise ValueError("Consulta fuera del contrato")
        if kind in {DatasetType.TRADE, DatasetType.MARK}:
            minute_ms(start)
            minute_ms(end)
        self.session, self.kind = session, kind
        self.start, self.end, self.cutoff = start, end, cutoff
        self.state = "PROVISIONAL"
        self.reason: str | None = None
        self.cursor = 0
        self.last_event: str | None = None
        self.current_segment: str | None = None
        self.cursors: dict[str, int] = {}
        self._economic, self._delivery = sha256(), sha256()
        self._closed = False
        self._temporary = tempfile.TemporaryDirectory(prefix="shib-query-preflight-")
        self.path = Path(self._temporary.name) / "interval.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * DEFAULT_LIMITS.max_json_bytes)
        self.db.execute("PRAGMA cache_size=-1024")
        self.db.executescript("""
CREATE TABLE inputs(dataset TEXT,sequence INTEGER,effective TEXT,available TEXT,valid_until TEXT,
 event_id TEXT,raw_digest TEXT,payload TEXT,segment TEXT,PRIMARY KEY(dataset,sequence)) STRICT;
CREATE INDEX input_time ON inputs(dataset,effective,event_id,sequence);
CREATE INDEX input_event ON inputs(dataset,event_id,sequence);
CREATE TABLE results(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL) STRICT;
""")
        try:
            roots = session.selected_roots(kind, start, end)
            self.verification = session.verify_segments(roots, self.db)
            streams = [
                self._source(identity)
                for identity in session.query.dataset_ids
                if session.specs[identity].declaration.dataset_type is kind
            ]
            fingerprint, count = sha256(), 0
            conflicts: list[str] = []
            for count, event in enumerate(_groups(streams), 1):
                if count > DEFAULT_LIMITS.max_rows:
                    raise ValueError("QUERY_ROW_LIMIT")
                value: dict[str, Any]
                if isinstance(event, HistoricalConflict):
                    if not allow_conflicts:
                        raise BlockedHistoricalInterval(event)
                    conflicts.append(digest(event))
                    if len(conflicts) > DEFAULT_LIMITS.max_findings:
                        raise ValueError("QUERY_CONFLICT_LIMIT")
                    value = {
                        "conflict": {
                            "effective_at": event.effective_at.isoformat(),
                            "end": event.end.isoformat(),
                            "dataset_ids": event.dataset_ids,
                            "event_ids": event.event_ids,
                            "reason": event.reason,
                        }
                    }
                else:
                    first = self.db.execute(
                        "SELECT payload FROM inputs WHERE dataset=? AND event_id=? "
                        "ORDER BY sequence LIMIT 1",
                        (event.dataset_ids[0], event.event_id),
                    ).fetchone()
                    cursors = tuple(
                        (
                            identity,
                            self.db.execute(
                                "SELECT max(sequence) FROM inputs WHERE dataset=? AND event_id=?",
                                (
                                    identity,
                                    event.event_id,
                                ),
                            ).fetchone()[0],
                        )
                        for identity in event.dataset_ids
                    )
                    segment = self.db.execute(
                        "SELECT segment FROM inputs WHERE dataset=? AND event_id=? "
                        "ORDER BY sequence LIMIT 1",
                        (event.dataset_ids[0], event.event_id),
                    ).fetchone()[0]
                    value = {
                        "event": {
                            "event_id": event.event_id,
                            "effective": event.market_effective_at.isoformat(),
                            "available": event.market_available_at.isoformat(),
                            "until": event.valid_until.isoformat(),
                            "raw": json.loads(first[0]),
                            "datasets": event.dataset_ids,
                            "sources": event.sources,
                            "provenance": event.provenance,
                            "duplicates": event.duplicates,
                            "cursors": cursors,
                            "segment": segment,
                        }
                    }
                payload = json_text(value)
                fingerprint.update(encoded((count, payload)))
                self.db.execute("INSERT INTO results VALUES(?,?)", (count, payload))
            self.count, self.preflight_digest = count, fingerprint.hexdigest()
            self.conflicts = tuple(conflicts)
            self.db.commit()
            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("BEGIN")
            self._rows = iter(
                self.db.execute("SELECT sequence,payload FROM results ORDER BY sequence")
            )
            session.check()
        except BaseException:
            self.state = "FAILED"
            self.close()
            raise

    def _source(self, identity: str) -> Iterator[HistoricalEvent]:
        spec = self.session.specs[identity]
        for row in self.db.execute(
            "SELECT * FROM inputs WHERE dataset=? AND effective<? AND valid_until>? "
            "AND available<=? AND effective<=? ORDER BY effective,event_id,sequence",
            (
                identity,
                self.end.isoformat(),
                self.start.isoformat(),
                self.cutoff.isoformat(),
                self.cutoff.isoformat(),
            ),
        ):
            if (
                self.kind in {DatasetType.TRADE, DatasetType.MARK, DatasetType.FUNDING}
                and utc_text(row[2]) < self.start
            ):
                continue
            raw = json.loads(row[7])
            record = parse_record(raw, self.kind, as_of=self.cutoff)
            if isinstance(record, (SymbolRules, HistoricalBrackets, FeeRules)) and (
                record.reference.quality is not RuleQuality.VERIFIED
                or record.reference.market_available_at > record.reference.valid_from
            ):
                raise ValueError("NOT_CAUSALLY_USABLE")
            yield HistoricalEvent(
                row[5],
                utc_text(row[2]),
                utc_text(row[3]),
                utc_text(row[4]),
                record,
                (identity,),
                (spec.declaration.source_url_declared,),
                (spec.provenance.value,),
            )

    def __enter__(self) -> "QueryStream[T]":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            if self.state == "PROVISIONAL":
                self.state = "INTERRUPTED"
            self._closed = True
            self.db.close()
            self._temporary.cleanup()

    def __next__(self) -> T:
        if self._closed:
            raise StopIteration
        try:
            self.session.check()
            row = next(self._rows, None)
            if row is None:
                if self.cursor != self.count or self._delivery.hexdigest() != self.preflight_digest:
                    raise ValueError("QUERY_SNAPSHOT_CHANGED")
                self.session.check()
                self.state = "COMPLETE"
                self.close()
                raise StopIteration
            sequence, payload = row
            if sequence != self.cursor + 1:
                raise ValueError("QUERY_CURSOR_CHANGED")
            self._delivery.update(encoded((sequence, payload)))
            data: dict[str, Any] = json.loads(payload)
            if "conflict" in data:
                item = data["conflict"]
                result: HistoricalEvent | HistoricalConflict = HistoricalConflict(
                    utc_text(item["effective_at"]),
                    utc_text(item["end"]),
                    tuple(item["dataset_ids"]),
                    tuple(item["event_ids"]),
                    item["reason"],
                )
            else:
                item = data["event"]
                record = parse_record(item["raw"], self.kind, as_of=self.cutoff)
                if economic_identity(item["raw"], self.kind, record) != item["event_id"]:
                    raise ValueError("QUERY_RECORD_CHANGED")
                result = HistoricalEvent(
                    item["event_id"],
                    utc_text(item["effective"]),
                    utc_text(item["available"]),
                    utc_text(item["until"]),
                    record,
                    tuple(item["datasets"]),
                    tuple(item["sources"]),
                    tuple(item["provenance"]),
                    item["duplicates"],
                )
                self._economic.update(encoded(result.economic_key()))
                self.last_event, self.current_segment = result.event_id, item["segment"]
                self.cursors.update(dict(item["cursors"]))
            self.cursor = sequence
            return cast(T, result)
        except StopIteration:
            raise
        except BaseException as exc:
            self.state, self.reason = "FAILED", str(exc)
            self.close()
            raise

    @property
    def causal_digest(self) -> str:
        return self._economic.hexdigest()

    def checkpoint(self) -> QueryRecoveryCheckpoint:
        if self.state == "FAILED":
            raise ValueError("FAILED_STREAM_NOT_CHECKPOINTABLE")
        cp = QueryRecoveryCheckpoint(
            self.verification,
            self.kind,
            self.start,
            self.end,
            self.cutoff,
            self.cursor,
            tuple(sorted(self.cursors.items())),
            self.current_segment,
            self.last_event,
            self.causal_digest,
            self.preflight_digest,
            self.conflicts,
            self.state,
            "",
        )
        cp = replace(cp, checkpoint_digest=digest(cp))
        if (
            cp.checkpoint_digest not in self.session.issued_checkpoints
            and len(self.session.issued_checkpoints) >= self.session.policy.max_queries
        ):
            raise ValueError("SESSION_CHECKPOINT_LIMIT")
        self.session.issued_checkpoints[cp.checkpoint_digest] = cp
        return cp


class SegmentedHistoricalQuery(HistoricalQuery):
    def __init__(self, session: SealedSegmentSession) -> None:
        self.session = session
        super().__init__(session.query.path, session.query.dataset_ids)

    def groups(
        self, kind: DatasetType, start: datetime, end: datetime, *, cutoff: datetime
    ) -> QueryStream[HistoricalEvent | HistoricalConflict]:
        return QueryStream[HistoricalEvent | HistoricalConflict](
            self.session, kind, start, end, cutoff, allow_conflicts=True
        )

    def read(
        self, kind: DatasetType, start: datetime, end: datetime, *, cutoff: datetime
    ) -> QueryStream[HistoricalEvent]:
        return QueryStream[HistoricalEvent](
            self.session, kind, start, end, cutoff, allow_conflicts=False
        )

    def resume(
        self,
        cp: QueryRecoveryCheckpoint,
        *,
        trusted_checkpoint_digest: str | None = None,
    ) -> QueryStream[HistoricalEvent]:
        self._validate_recovery_root(cp, trusted_checkpoint_digest)
        return self._restore(cp, self.read(cp.kind, cp.start, cp.end, cutoff=cp.cutoff))

    def resume_groups(
        self,
        cp: QueryRecoveryCheckpoint,
        *,
        trusted_checkpoint_digest: str | None = None,
    ) -> QueryStream[HistoricalEvent | HistoricalConflict]:
        self._validate_recovery_root(cp, trusted_checkpoint_digest)
        return self._restore(cp, self.groups(cp.kind, cp.start, cp.end, cutoff=cp.cutoff))

    def _validate_recovery_root(
        self, cp: QueryRecoveryCheckpoint, trusted_checkpoint_digest: str | None
    ) -> None:
        self.session.check()
        if digest(replace(cp, checkpoint_digest="")) != cp.checkpoint_digest:
            raise ValueError("CHECKPOINT_DIGEST_CHANGED")
        local = self.session.issued_checkpoints.get(cp.checkpoint_digest)
        if local != cp and trusted_checkpoint_digest != cp.checkpoint_digest:
            raise ValueError("INDEPENDENT_CHECKPOINT_ROOT_REQUIRED")
        if cp.stream_state not in {"PROVISIONAL", "INTERRUPTED", "COMPLETE"}:
            raise ValueError("CHECKPOINT_STATE_UNSUPPORTED")

    def _restore[U: HistoricalEvent | HistoricalConflict](
        self, cp: QueryRecoveryCheckpoint, stream: QueryStream[U]
    ) -> QueryStream[U]:
        try:
            if cp.cursor > stream.count:
                raise ValueError("FUTURE_CURSOR")
            for _ in range(cp.cursor):
                next(stream)
            expected = stream.checkpoint()
            verification = replace(
                expected.verification, session_identity=cp.verification.session_identity
            )
            expected = replace(
                expected,
                verification=verification,
                stream_state=cp.stream_state,
                checkpoint_digest="",
            )
            if replace(cp, checkpoint_digest="") != expected:
                raise ValueError("CHECKPOINT_CONTEXT_CHANGED")
            if cp.stream_state == "COMPLETE" and cp.cursor != stream.count:
                raise ValueError("CHECKPOINT_INCOMPLETE_CURSOR")
            return stream
        except BaseException:
            stream.close()
            raise


def historical_query(
    path: Path,
    dataset_ids: tuple[str, ...],
    *,
    mode: VerificationMode = VerificationMode.STRICT_FULL,
    session: SealedSegmentSession | None = None,
) -> HistoricalQuery:
    if mode is VerificationMode.STRICT_FULL:
        if session is not None:
            raise ValueError("STRICT_FULL no reutiliza sesiones")
        return HistoricalQuery(path, dataset_ids)
    if mode is not VerificationMode.SEALED_SEGMENT_SESSION or session is None:
        raise ValueError("Modo rápido exige sesión explícita")
    reference = HistoricalQuery(path, dataset_ids)
    if session.query.path != reference.path or session.query.selection_id != reference.selection_id:
        raise ValueError("SESSION_SELECTION_CHANGED")
    return SegmentedHistoricalQuery(session)
