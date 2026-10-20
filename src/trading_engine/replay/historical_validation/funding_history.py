"""Funding preparado para revisión causal; tasas e inventarios no son pagos."""

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from trading_engine.binance.klines import LAUNCH
from trading_engine.replay.encoding import encoded
from trading_engine.utils.time import require_utc

from .audit import FundingInventory
from .contracts import DEFAULT_LIMITS, Contract, DatasetType, exact_decimal, utc_text
from .fast_query import QueryStream, historical_query
from .parsers import FundingRate
from .query import BlockedHistoricalInterval, HistoricalEvent
from .reconciliation import HistoricalReconciliation, ReconciliationReport
from .registry import decode_registration, digest
from .replay_adapter import DEFAULT_ADAPTER_LIMITS, AdapterLimits
from .segment_session import SealedSegmentSession
from .segments import Registration
from .storage import connection
from .verification import VerificationCheckpoint, VerificationMode, VerificationState

BLOCK_ROWS = 1440


@dataclass(frozen=True)
class FundingBinding:
    dataset_id: str
    audit_id: str
    source_digest: str
    registration: Registration


@dataclass(frozen=True)
class FundingBlock:
    first_sequence: int
    last_sequence: int
    first_available_at: datetime
    content_digest: str


@dataclass(frozen=True)
class PreparedFundingRow:
    sequence: int
    event: HistoricalEvent


@dataclass(frozen=True)
class PreparedFundingEvidence:
    start: datetime
    end: datetime
    cutoff: datetime
    mode: VerificationMode
    bindings: tuple[FundingBinding, ...]
    reconciliation: ReconciliationReport
    inventory: FundingInventory | None
    inventory_identity: str | None
    inventory_status: str
    reference_status: str
    verification: VerificationCheckpoint | None
    source_state: VerificationState
    original_state: VerificationState
    identity: str
    economic_identity: str
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
    economic_use: str = "HISTORICAL_RATE_NOT_USER_PAYMENT"


def bindings(
    path: Path, ids: tuple[str, ...], session: SealedSegmentSession | None
) -> tuple[FundingBinding, ...]:
    result: list[FundingBinding] = []
    if session is not None:
        session.check()
        result = [
            FundingBinding(i, session.audit_ids[i], session.source_hashes[i], session.specs[i])
            for i in ids
        ]
    else:
        with connection(path) as db:
            for identity in ids:
                row = db.execute(
                    "SELECT spec,audit_id,source_hash,state FROM datasets WHERE id=?", (identity,)
                ).fetchone()
                if row is None or row[3] != "COMPLETE_IMPORT":
                    raise ValueError("FUNDING_DATASET_NOT_COMPLETE")
                result.append(FundingBinding(identity, row[1], row[2], decode_registration(row[0])))
    for binding in result:
        declaration = binding.registration.declaration
        if (
            declaration.dataset_type is not DatasetType.FUNDING
            or declaration.contract != Contract()
            or declaration.interval != "EVENTS"
        ):
            raise ValueError("FUNDING_DATASETS_REQUIRED")
    return tuple(result)


def decode(payload: str) -> HistoricalEvent:
    data: dict[str, Any] = json.loads(payload)
    raw = data["record"]
    record = FundingRate(
        utc_text(raw["market_effective_at"]),
        utc_text(raw["market_available_at"]),
        exact_decimal(raw["rate"], "fundingRate"),
        None if raw["mark_price"] is None else exact_decimal(raw["mark_price"], "markPrice"),
        raw["rate_type"],
        raw["economic_use"],
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


class PreparedFundingHistory:
    def __init__(
        self,
        path: Path,
        dataset_ids: tuple[str, ...],
        start: datetime,
        end: datetime,
        *,
        cutoff: datetime,
        inventory: FundingInventory | None,
        mode: VerificationMode = VerificationMode.STRICT_FULL,
        session: SealedSegmentSession | None = None,
        limits: AdapterLimits = DEFAULT_ADAPTER_LIMITS,
    ) -> None:
        for at in (start, end, cutoff):
            require_utc(at)
        if start < LAUNCH or start >= end or cutoff <= start:
            raise ValueError("NO_VERIFIED_FUNDING_PREFIX")
        if not isinstance(limits, AdapterLimits):
            raise TypeError("AdapterLimits requerido")
        if type(dataset_ids) is not tuple or not dataset_ids:
            raise ValueError("EXPLICIT_FUNDING_DATASET_IDS_REQUIRED")
        if inventory is not None and not isinstance(inventory, FundingInventory):
            raise TypeError("FundingInventory explícito requerido")
        if inventory is not None and (inventory.start != start or inventory.end != end):
            raise ValueError("FUNDING_INVENTORY_INTERVAL_MISMATCH")
        if inventory is not None and len(inventory.expected_timestamps) > limits.max_rows:
            raise ValueError("FUNDING_INVENTORY_LIMIT")
        query = historical_query(path, dataset_ids, mode=mode, session=session)
        selected = bindings(query.path, query.dataset_ids, session)
        declared = [b.registration.funding_inventory for b in selected]
        if any(value is not None and value != inventory for value in declared):
            raise ValueError("INCOMPATIBLE_FUNDING_INVENTORIES")
        self.start, self.end, self.cutoff = start, end, cutoff
        self._source_session = session
        self._token = object()
        self._ready = False
        self._db: sqlite3.Connection | None = None
        self._temporary = tempfile.TemporaryDirectory(prefix="shib-prepared-funding-")
        self.path = Path(self._temporary.name) / "funding.sqlite3"
        self.blocks: tuple[FundingBlock, ...] = ()
        self._cached: FundingBlock | None = None
        self._rows: tuple[PreparedFundingRow, ...] = ()
        self.metrics: dict[str, float | int] = {}
        begun = perf_counter()
        try:
            reconciliation = HistoricalReconciliation(query).evaluate(
                DatasetType.FUNDING, start, end, cutoff=cutoff, funding_inventory=inventory
            )
            if reconciliation.conflicts:
                raise BlockedHistoricalInterval(reconciliation.conflicts[0])
            stream = query.read_funding(start, end, cutoff=cutoff)
            verification = stream.verification if isinstance(stream, QueryStream) else None
            try:
                count, duplicates, fingerprint, matched, references = self._stage(
                    stream, inventory, limits
                )
                if isinstance(stream, QueryStream) and (
                    stream.state != "COMPLETE" or stream.causal_digest != fingerprint
                ):
                    raise ValueError("FUNDING_QUERY_NOT_COMPLETE")
            finally:
                if hasattr(stream, "close"):
                    stream.close()
            if (
                count != reconciliation.recognized
                or duplicates != reconciliation.duplicates
                or fingerprint != reconciliation.causal_digest
                or selected != bindings(query.path, query.dataset_ids, session)
            ):
                raise ValueError("FUNDING_RECONCILIATION_CHANGED")
            status = (
                "NOT_AVAILABLE"
                if inventory is None or inventory.market_available_at > cutoff
                else "COMPLETE"
                if matched
                else "INCOMPLETE"
            )
            self.row_count = count
            identity = digest(
                ("prepared-funding-v1", start, end, cutoff, mode, selected, inventory, self.blocks)
            )
            self.evidence = PreparedFundingEvidence(
                start,
                end,
                cutoff,
                mode,
                selected,
                reconciliation,
                inventory,
                None if inventory is None else digest((inventory, selected)),
                status,
                "COMPLETE" if references else "INCOMPLETE",
                verification,
                VerificationState.FULL_SOURCE_VERIFIED
                if verification is None
                else verification.source_state,
                VerificationState.FULL_SOURCE_VERIFIED
                if verification is None
                else verification.original_state,
                identity,
                digest(("funding-inputs-v1", start, end, cutoff, selected, inventory, fingerprint)),
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
        self,
        stream: Iterator[HistoricalEvent],
        inventory: FundingInventory | None,
        limits: AdapterLimits,
    ) -> tuple[int, int, str, bool, bool]:
        fingerprint = sha256()
        count = duplicates = 0
        matched = inventory is not None
        references = True
        with closing(sqlite3.connect(self.path)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, DEFAULT_LIMITS.max_json_bytes)
            db.execute("PRAGMA page_size=4096")
            db.execute(f"PRAGMA max_page_count={limits.max_temporary_bytes // 4096}")
            db.execute("PRAGMA cache_size=-1024")
            db.execute("PRAGMA temp_store=FILE")
            db.execute("CREATE TABLE staging(effective TEXT,available TEXT,payload TEXT) STRICT")
            for count, event in enumerate(stream, 1):
                record = event.record
                if (
                    not isinstance(record, FundingRate)
                    or not self.start <= event.market_effective_at < self.end
                    or not event.market_effective_at <= event.market_available_at <= self.cutoff
                    or record.market_effective_at != event.market_effective_at
                    or record.market_available_at != event.market_available_at
                    or record.economic_use != "HISTORICAL_RATE_NOT_USER_PAYMENT"
                    or event.authentication != "NOT_AUTHENTICATED"
                    or event.economic_authorization != "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
                ):
                    raise ValueError("INVALID_FUNDING_EVENT")
                if count > limits.max_rows:
                    raise ValueError("FUNDING_ROW_LIMIT")
                if inventory is not None and (
                    count > len(inventory.expected_timestamps)
                    or inventory.expected_timestamps[count - 1] != event.market_effective_at
                ):
                    matched = False
                references = references and record.mark_price is not None
                fingerprint.update(encoded(event.economic_key()))
                duplicates += event.duplicates
                db.execute(
                    "INSERT INTO staging VALUES(?,?,?)",
                    (
                        event.market_effective_at.isoformat(),
                        event.market_available_at.isoformat(),
                        encoded(event).decode(),
                    ),
                )
            matched = bool(
                matched and inventory is not None and count == len(inventory.expected_timestamps)
            )
            db.execute(
                "CREATE TABLE funding(sequence INTEGER PRIMARY KEY,available TEXT NOT NULL,"
                "payload TEXT NOT NULL) STRICT"
            )
            db.execute(
                "INSERT INTO funding SELECT ROW_NUMBER() OVER (ORDER BY available,effective),"
                "available,payload FROM staging ORDER BY available,effective"
            )
            db.execute("DROP TABLE staging")
            roots: list[FundingBlock] = []
            block_hash = sha256()
            first_available = self.start
            for sequence, available, payload in db.execute(
                "SELECT * FROM funding ORDER BY sequence"
            ):
                if (sequence - 1) % BLOCK_ROWS == 0:
                    first_available = utc_text(available)
                block_hash.update(encoded((sequence, available, payload)))
                if sequence % BLOCK_ROWS == 0 or sequence == count:
                    roots.append(
                        FundingBlock(
                            (sequence - 1) // BLOCK_ROWS * BLOCK_ROWS + 1,
                            sequence,
                            first_available,
                            block_hash.hexdigest(),
                        )
                    )
                    block_hash = sha256()
            db.commit()
        if self.path.stat().st_size > limits.max_temporary_bytes:
            raise ValueError("FUNDING_STORAGE_LIMIT")
        self.blocks = tuple(roots)
        return count, duplicates, fingerprint.hexdigest(), matched, references

    def _open_snapshot(self) -> None:
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        self._db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, DEFAULT_LIMITS.max_json_bytes)
        self._db.execute("PRAGMA query_only=ON")
        self._db.execute("PRAGMA cache_size=-1024")
        if self._db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("FUNDING_SNAPSHOT_CORRUPT")
        self._db.execute("BEGIN")
        self._db.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()

    def check(self) -> None:
        if not self._ready:
            raise ValueError("PREPARED_FUNDING_NOT_AVAILABLE")
        if self._source_session is not None:
            self._source_session.check()

    @property
    def session(self) -> object:
        self.check()
        return self._token

    def _block(self, root: FundingBlock) -> tuple[PreparedFundingRow, ...]:
        self.check()
        if self._cached == root:
            return self._rows
        assert self._db is not None
        try:
            rows = tuple(
                self._db.execute(
                    "SELECT * FROM funding WHERE sequence BETWEEN ? AND ? ORDER BY sequence",
                    (root.first_sequence, root.last_sequence),
                )
            )
            fingerprint = sha256()
            for index, row in enumerate(rows):
                if row[0] != root.first_sequence + index:
                    raise ValueError("FUNDING_CURSOR_CHANGED")
                fingerprint.update(encoded(row))
            if (
                len(rows) != root.last_sequence - root.first_sequence + 1
                or fingerprint.hexdigest() != root.content_digest
            ):
                raise ValueError("FUNDING_BLOCK_CHANGED")
            # No interpretar JSON antes de verificar íntegro el bloque.
            self._rows = tuple(PreparedFundingRow(row[0], decode(row[2])) for row in rows)
        except BaseException:
            self.close()
            raise
        self._cached = root
        return self._rows

    def read(self, cursor: int, until: datetime) -> Iterator[PreparedFundingRow]:
        self.check()
        require_utc(until)
        if type(cursor) is not int or not 0 <= cursor <= self.row_count:
            raise ValueError("FUNDING_CURSOR_INVALID")
        if not self.start <= until <= self.cutoff:
            raise ValueError("FUNDING_BEYOND_CUTOFF")
        for root in self.blocks:
            if root.last_sequence <= cursor:
                continue
            if root.first_available_at > until:
                break
            for row in self._block(root):
                self.check()
                if row.event.market_available_at > until:
                    self.check()
                    return
                if row.sequence > cursor:
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

    def __enter__(self) -> "PreparedFundingHistory":
        self.check()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
