"""Historia Trade preparada atómicamente; no convierte históricos en evidencia financiera."""

import json
import sqlite3
import tempfile
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import localcontext
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, from_ms, minute_ms, to_ms
from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.models import ReplayRequest
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.utils.time import require_utc

from .contracts import DEFAULT_LIMITS, Contract, DatasetType, exact_decimal, utc_text
from .fast_query import QueryStream, historical_query
from .query import BlockedHistoricalInterval, HistoricalEvent
from .reconciliation import HistoricalReconciliation, ReconciliationReport
from .registry import decode_registration, digest
from .segment_session import SealedSegmentSession
from .segments import PARSER_VERSION, SCHEMA_VERSION, Registration
from .storage import connection
from .verification import VerificationCheckpoint, VerificationMode, VerificationState

BLOCK_ROWS = 1440
MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class AdapterLimits:
    """Cuotas locales, sin parámetros de mercado ni configuración financiera."""

    max_rows: int = DEFAULT_LIMITS.max_rows
    max_temporary_bytes: int = DEFAULT_LIMITS.max_file_bytes

    def __post_init__(self) -> None:
        if type(self.max_rows) is not int or not 0 < self.max_rows <= DEFAULT_LIMITS.max_rows:
            raise ValueError("ADAPTER_ROW_LIMIT_INVALID")
        if (
            type(self.max_temporary_bytes) is not int
            or not 16384 <= self.max_temporary_bytes <= DEFAULT_LIMITS.max_file_bytes
        ):
            raise ValueError("ADAPTER_STORAGE_LIMIT_INVALID")


DEFAULT_ADAPTER_LIMITS = AdapterLimits()


@dataclass(frozen=True)
class TradeSourceBinding:
    dataset_id: str
    audit_id: str
    source_digest: str
    registration: Registration


@dataclass(frozen=True)
class PreparedTradeEvidence:
    request: ReplayRequest
    query_start: datetime
    cutoff: datetime
    mode: VerificationMode
    bindings: tuple[TradeSourceBinding, ...]
    reconciliation: ReconciliationReport
    verification: VerificationCheckpoint | None
    source_state: VerificationState
    original_state: VerificationState
    query_digest: str
    candle_digest: str
    warmup_read: int
    warmup_consecutive: int
    warmup_status: str
    coverage_status: str
    provenances: tuple[str, ...]
    identity: str
    schema_version: int = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
    trade_use: str = "OFFLINE_REPLAY_CANDIDATE"
    historical_financial_adaptation: str = "NOT_AUTHORIZED"
    historical_execution_evidence: str = "NOT_AVAILABLE"
    financial_categories: tuple[str, ...] = (
        "MARK",
        "FUNDING",
        "FEES",
        "BRACKETS",
        "CAPITAL",
        "REAL_EXECUTION",
    )


@dataclass(frozen=True)
class PreparedBlock:
    first_sequence: int
    last_sequence: int
    start: datetime
    end: datetime
    content_digest: str


def _bindings(
    path: Path, ids: tuple[str, ...], session: SealedSegmentSession | None
) -> tuple[TradeSourceBinding, ...]:
    if session is not None:
        session.check()
        values = tuple(
            TradeSourceBinding(i, session.audit_ids[i], session.source_hashes[i], session.specs[i])
            for i in ids
        )
    else:
        with connection(path) as db:
            saved: list[TradeSourceBinding] = []
            for identity in ids:
                row = db.execute(
                    "SELECT spec,audit_id,source_hash,state FROM datasets WHERE id=?", (identity,)
                ).fetchone()
                if row is None:
                    raise ValueError("DATASET_NOT_PROVIDED")
                if row[3] != "COMPLETE_IMPORT":
                    raise ValueError("DATASET_NOT_COMPLETE")
                saved.append(
                    TradeSourceBinding(identity, row[1], row[2], decode_registration(row[0]))
                )
            values = tuple(saved)
    for binding in values:
        declaration = binding.registration.declaration
        if (
            declaration.dataset_type is not DatasetType.TRADE
            or declaration.contract != Contract()
            or declaration.interval != "1m"
        ):
            raise ValueError("TRADE_DATASETS_REQUIRED")
    return values


def _candle(text: str) -> Candle:
    data: dict[str, Any] = json.loads(text)[0]
    with localcontext(ledger_context()):
        return Candle(
            Symbol(data["symbol"]["value"]),
            utc_text(data["open_time"]),
            utc_text(data["close_time"]),
            Price(exact_decimal(data["open"]["value"], "open")),
            Price(exact_decimal(data["high"]["value"], "high")),
            Price(exact_decimal(data["low"]["value"], "low")),
            Price(exact_decimal(data["close"]["value"], "close")),
            exact_decimal(data["volume"], "volume", nonnegative=True),
            None
            if data["taker_buy_base_volume"] is None
            else exact_decimal(data["taker_buy_base_volume"], "taker_buy", nonnegative=True),
        )


class PreparedTradeHistory:
    """Sólo publica Candle de una lectura terminada, conciliada y sellada en memoria."""

    def __init__(
        self,
        path: Path,
        dataset_ids: tuple[str, ...],
        request: ReplayRequest,
        *,
        mode: VerificationMode = VerificationMode.STRICT_FULL,
        session: SealedSegmentSession | None = None,
        cutoff: datetime | None = None,
        limits: AdapterLimits = DEFAULT_ADAPTER_LIMITS,
    ) -> None:
        if type(dataset_ids) is not tuple or not dataset_ids:
            raise ValueError("EXPLICIT_TRADE_DATASET_IDS_REQUIRED")
        if not isinstance(request, ReplayRequest) or not isinstance(limits, AdapterLimits):
            raise TypeError("ReplayRequest y AdapterLimits requeridos")
        cutoff = request.end if cutoff is None else require_utc(cutoff)
        start = max(request.warmup_start(AltBConfig()), LAUNCH)
        end = from_ms(to_ms(min(request.end, cutoff)) // 60000 * 60000)
        if start >= end:
            raise ValueError("NO_VERIFIED_REPLAY_PREFIX")
        if (minute_ms(end) - minute_ms(start)) // 60000 > limits.max_rows:
            raise ValueError("ADAPTER_ROW_LIMIT")
        query = historical_query(path, dataset_ids, mode=mode, session=session)
        self.request, self.cutoff, self.start, self.end = request, cutoff, start, end
        self._source_session = session
        self._token = object()
        self._ready = False
        self._closed = False
        self._db: sqlite3.Connection | None = None
        self._cached_block: PreparedBlock | None = None
        self._candles: dict[datetime, Candle] = {}
        self.blocks: tuple[PreparedBlock, ...] = ()
        self.metrics: dict[str, float | int] = {}
        self._temporary = tempfile.TemporaryDirectory(prefix="shib-prepared-trade-")
        self.path = Path(self._temporary.name) / "prepared.sqlite3"
        begun = perf_counter()
        try:
            bindings = _bindings(query.path, query.dataset_ids, session)
            validating = perf_counter()
            reconciliation = HistoricalReconciliation(query).evaluate(
                DatasetType.TRADE, start, request.end, cutoff=cutoff
            )
            self.metrics["validation_seconds"] = perf_counter() - validating
            if reconciliation.conflicts:
                raise BlockedHistoricalInterval(reconciliation.conflicts[0])
            stream = query.read_trade(start, end, cutoff=cutoff)
            try:
                if mode is VerificationMode.SEALED_SEGMENT_SESSION and not isinstance(
                    stream, QueryStream
                ):
                    raise ValueError("QUERY_STREAM_REQUIRED")
                verification = stream.verification if isinstance(stream, QueryStream) else None
                query_hash, candle_hash = self._stage(stream, limits)
                if isinstance(stream, QueryStream):
                    if stream.state != "COMPLETE":
                        raise ValueError("QUERY_STREAM_NOT_COMPLETE:" + stream.state)
                    if stream.causal_digest != query_hash:
                        raise ValueError("QUERY_STREAM_DIGEST_MISMATCH")
                if (
                    query_hash != reconciliation.causal_digest
                    or self.metrics["rows"] != reconciliation.recognized
                    or self.metrics["duplicates"] != reconciliation.duplicates
                ):
                    raise ValueError("RECONCILIATION_QUERY_MISMATCH")
            finally:
                if isinstance(stream, (Generator, QueryStream)):
                    stream.close()
            if bindings != _bindings(query.path, query.dataset_ids, session):
                raise ValueError("SOURCE_BINDING_CHANGED")
            self._open_snapshot()
            warmup = int(self.metrics["warmup_read"])
            consecutive = int(self.metrics["warmup_consecutive"])
            identity = digest((request, cutoff, mode, bindings, reconciliation, self.blocks))
            self.evidence = PreparedTradeEvidence(
                request,
                start,
                cutoff,
                mode,
                bindings,
                reconciliation,
                verification,
                VerificationState.FULL_SOURCE_VERIFIED
                if verification is None
                else verification.source_state,
                VerificationState.FULL_SOURCE_VERIFIED
                if verification is None
                else verification.original_state,
                query_hash,
                candle_hash,
                warmup,
                consecutive,
                "AVAILABLE" if consecutive == 1560 else "INCOMPLETE",
                "AVAILABLE"
                if reconciliation.coverage_status == "AVAILABLE_FOR_REVIEW" and end == request.end
                else "INCOMPLETE",
                tuple(sorted({b.registration.provenance.value for b in bindings})),
                identity,
            )
            self.metrics["temporary_bytes"] = self.path.stat().st_size
            self.metrics["preparation_seconds"] = perf_counter() - begun
            self._ready = True
        except BaseException:
            self.close()
            raise

    def _stage(self, stream: Iterator[HistoricalEvent], limits: AdapterLimits) -> tuple[str, str]:
        writer = sqlite3.connect(self.path)
        roots: list[PreparedBlock] = []
        fingerprint, candles, block_hash = sha256(), sha256(), sha256()
        count = duplicates = warmup = consecutive = 0
        previous: datetime | None = None
        block_start: datetime | None = None
        block_end: datetime | None = None
        try:
            writer.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, DEFAULT_LIMITS.max_json_bytes)
            writer.execute("PRAGMA page_size=4096")
            writer.execute(f"PRAGMA max_page_count={limits.max_temporary_bytes // 4096}")
            writer.execute("PRAGMA cache_size=-1024")
            writer.execute("PRAGMA foreign_keys=ON")
            writer.execute(
                "CREATE TABLE candles(sequence INTEGER PRIMARY KEY,"
                "opened INTEGER UNIQUE NOT NULL,available INTEGER NOT NULL,"
                "payload TEXT NOT NULL) STRICT"
            )
            writer.execute("BEGIN")
            for count, event in enumerate(stream, 1):
                candle = event.record
                if count > limits.max_rows:
                    raise ValueError("ADAPTER_ROW_LIMIT")
                if (
                    not isinstance(candle, Candle)
                    or candle.symbol != DEFAULT_SYMBOL
                    or candle.open_time != event.market_effective_at
                    or candle.close_time != event.market_available_at
                    or candle.close_time != event.valid_until
                    or candle.close_time != candle.open_time + MINUTE
                    or candle.open_time < self.start
                    or candle.close_time > self.end
                    or (previous is not None and candle.open_time <= previous)
                    or event.authentication != "NOT_AUTHENTICATED"
                    or event.economic_authorization != "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
                ):
                    raise ValueError("INVALID_REPLAY_TRADE_EVENT")
                payload = encoded(
                    (
                        candle,
                        event.economic_key(),
                        event.dataset_ids,
                        event.sources,
                        event.provenance,
                        event.duplicates,
                    )
                ).decode()
                row = (count, minute_ms(candle.open_time), minute_ms(candle.close_time), payload)
                writer.execute("INSERT INTO candles VALUES(?,?,?,?)", row)
                fingerprint.update(encoded(event.economic_key()))
                candles.update(encoded((count, candle)))
                block_hash.update(encoded(row))
                duplicates += event.duplicates
                if block_start is None:
                    block_start = candle.open_time
                block_end = candle.close_time
                if candle.close_time <= self.request.start:
                    warmup += 1
                    consecutive = consecutive + 1 if previous == candle.open_time - MINUTE else 1
                previous = candle.open_time
                if count % BLOCK_ROWS == 0:
                    roots.append(
                        PreparedBlock(
                            count - BLOCK_ROWS + 1,
                            count,
                            block_start,
                            block_end,
                            block_hash.hexdigest(),
                        )
                    )
                    block_start, block_hash = None, sha256()
                if count % BLOCK_ROWS == 0:
                    size = sum(p.stat().st_size for p in self.path.parent.iterdir() if p.is_file())
                    if size > limits.max_temporary_bytes:
                        raise ValueError("ADAPTER_STORAGE_LIMIT")
            if block_start is not None:
                assert block_end is not None
                roots.append(
                    PreparedBlock(
                        len(roots) * BLOCK_ROWS + 1,
                        count,
                        block_start,
                        block_end,
                        block_hash.hexdigest(),
                    )
                )
            writer.commit()
            if self.path.stat().st_size > limits.max_temporary_bytes:
                raise ValueError("ADAPTER_STORAGE_LIMIT")
            self.blocks = tuple(roots)
            # El warmup debe terminar exactamente en start; contar extremos no prueba continuidad.
            last = writer.execute(
                "SELECT max(available) FROM candles WHERE opened<?",
                (minute_ms(self.request.start),),
            ).fetchone()[0]
            self.metrics.update(
                rows=count,
                duplicates=duplicates,
                warmup_read=warmup,
                warmup_consecutive=min(1560, consecutive)
                if last == minute_ms(self.request.start)
                else 0,
            )
            return fingerprint.hexdigest(), candles.hexdigest()
        finally:
            writer.close()

    def _open_snapshot(self) -> None:
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        self._db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, DEFAULT_LIMITS.max_json_bytes)
        self._db.execute("PRAGMA query_only=ON")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA cache_size=-1024")
        if self._db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("PREPARED_HISTORY_CORRUPT")
        self._db.execute("BEGIN")
        self._db.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()

    def _check(self) -> None:
        if self._closed or not self._ready:
            raise ValueError("PREPARED_HISTORY_NOT_AVAILABLE")
        if self._source_session is not None:
            self._source_session.check()

    @property
    def session(self) -> object:
        self._check()
        return self._token

    def __enter__(self) -> "PreparedTradeHistory":
        self._check()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._ready = False
            self._candles.clear()
            if self._db is not None:
                self._db.close()
            self._temporary.cleanup()

    def conflicts(self, symbol: Symbol) -> tuple[str, ...]:
        self._check()
        if symbol != DEFAULT_SYMBOL:
            raise ValueError("REPLAY_CONTRACT_MISMATCH")
        return ()  # La preparación bloqueó todo conflicto antes de publicar la historia.

    def _block(self, root: PreparedBlock) -> dict[datetime, Candle]:
        if self._cached_block == root:
            return self._candles
        assert self._db is not None
        fingerprint = sha256()
        values: dict[datetime, Candle] = {}
        count = 0
        for row in self._db.execute(
            "SELECT sequence,opened,available,payload FROM candles "
            "WHERE sequence BETWEEN ? AND ? ORDER BY sequence",
            (root.first_sequence, root.last_sequence),
        ):
            if row[0] != root.first_sequence + count:
                raise ValueError("PREPARED_HISTORY_CURSOR_CHANGED")
            fingerprint.update(encoded(row))
            candle = _candle(row[3])
            if (minute_ms(candle.open_time), minute_ms(candle.close_time)) != (row[1], row[2]):
                raise ValueError("PREPARED_HISTORY_TIME_CHANGED")
            values[candle.open_time] = candle
            count += 1
        if (
            count != root.last_sequence - root.first_sequence + 1
            or fingerprint.hexdigest() != root.content_digest
        ):
            raise ValueError("PREPARED_HISTORY_BLOCK_CHANGED")
        self._cached_block, self._candles = root, values
        return values

    def read(self, symbol: Symbol, start: datetime, end: datetime) -> Generator[Candle, None, None]:
        self.conflicts(symbol)
        minute_ms(start)
        require_utc(end)
        if (
            start >= end
            or start < self.request.warmup_start(AltBConfig())
            or end > self.request.end
        ):
            raise ValueError("REPLAY_OUTSIDE_PREPARED_REQUEST")
        if end > self.cutoff:
            raise ValueError("REPLAY_BEYOND_VERIFIED_CUTOFF")
        for root in self.blocks:
            if root.start >= end or root.end <= start:
                continue
            try:
                block = self._block(root)
            except (ValueError, TypeError, KeyError, sqlite3.Error):
                self.close()
                raise
            opened = max(start, root.start)
            boundary = min(end, root.end)
            while opened + MINUTE <= boundary:
                self._check()
                candle = block.get(opened)
                if candle is not None:
                    yield candle
                opened += MINUTE
        self._check()
