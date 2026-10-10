"""SYNTHETIC_FIXTURE exclusivamente; equivalencia, causalidad y publicación atómica."""

import json
import socket
import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, Inexact, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from trading_engine.binance.klines import LAUNCH, to_ms
from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.replay.historical_validation.contracts import DatasetType
from trading_engine.replay.historical_validation.fast_query import QueryStream
from trading_engine.replay.historical_validation.query import HistoricalEvent, HistoricalQuery
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.replay_adapter import (
    AdapterLimits,
    PreparedTradeHistory,
)
from trading_engine.replay.historical_validation.segments import Registration, SegmentBounds
from trading_engine.replay.historical_validation.verification import (
    VerificationMode,
    VerificationState,
)
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayRequest
from trading_engine.replay.ports import FixtureFacts, FixtureQuotes, NoQuotes
from trading_engine.replay.runner import ReplayRunner
from trading_engine.strategy.alt_b.config import AltBConfig

from .conftest import DUE, END, START, SYMBOL, T
from .test_historical_fast_query import registered, session
from .test_historical_registry import kline, rule, spec

MODE = VerificationMode.SEALED_SEGMENT_SESSION
MINUTE = timedelta(minutes=1)

# Congelados antes de editar runner/ports; tres ejecuciones de main ab6a8dd.
BASELINES = {
    "no_quotes": (
        "1de51441efde90d5059802f62aac9fb21926a16be4dd23b1327109f352d96746",
        "f0b712d5dca3e1f4fb8262766aead74b59ffb2299fb4650a9a00408421ef3b06",
        "c9ee42b7d24f20a80aba2b6acc2c7c08dcaf8934e159b0d9890afd8ffc216f52",
        "b2c1809c2fa7cd913f2401cb6363244aa38ba7e0dd1bd17e0208bd124da9b15c",
    ),
    "quotes": (
        "2ca54f97b544a9f06248d4f5144eaa58dc2f90573ad2f57204c371b1fa074b47",
        "8fbb90af2be76e34011548013cf0c11919bf2c2f0aa35b5b62ec189a225854f0",
        "082d6b99cc2b79066beea2cedc9fa479089f9811189e8a6c972d7fd96529a231",
        "9242a08b84d2271c06d3cba287b23cf41ee58ef08e74d6c456be6fae39044437",
    ),
    "facts": (
        "281e69e645499ff683d854d47844292a026279bacf5902e12d11be986746f484",
        "e81a58a9f7a5b371c1217e825feae63f00749e84ea9344760876b4c419a39953",
        "e186ab3228eb65cfa04c58626d8d4317413884601dcf0d2005ff9a8624665f1d",
        "98e8b0daaaff86838b4eb978e9428eb6fabd0dc8e41ad4b9a0b620f3808e791e",
    ),
}


def trade_rows(candles: tuple[Candle, ...]) -> list[object]:
    return [
        [
            to_ms(c.open_time),
            str(c.open.value),
            str(c.high.value),
            str(c.low.value),
            str(c.close.value),
            str(c.volume),
            to_ms(c.close_time) - 1,
            "1000",
            2,
            str(c.taker_buy_base_volume),
            "500",
            "0",
        ]
        for c in candles
    ]


def trade_registration(folder: Path, candles: tuple[Candle, ...]) -> Registration:
    value = spec(
        folder,
        "synthetic-trade",
        trade_rows(candles),
        start=candles[0].open_time,
        end=candles[-1].close_time,
    )
    value = replace(
        value,
        declaration=replace(value.declaration, acquisition_time=END + timedelta(days=1)),
        validation_time=END + timedelta(days=2),
    )
    left, end = value.declaration.period_start, value.declaration.period_end
    segments: list[SegmentBounds] = []
    while left < end:
        right = min(left + timedelta(days=1), end)
        segments.append(SegmentBounds(left, right))
        left = right
    return replace(value, segments=tuple(segments))


@pytest.fixture(scope="module")
def trade_registry(
    tmp_path_factory: pytest.TempPathFactory, candles: tuple[Candle, ...]
) -> tuple[Path, tuple[str, ...]]:
    directory = tmp_path_factory.mktemp("adapter-registry")
    return registered(directory, trade_registration(directory, candles))


def small_registry(tmp_path: Path) -> tuple[Path, tuple[str, ...], ReplayRequest, Registration]:
    registration = spec(tmp_path, "small", [kline(i) for i in range(3)])
    path, ids = registered(tmp_path, registration)
    return path, ids, ReplayRequest(LAUNCH, LAUNCH + 3 * MINUTE), registration


@pytest.mark.parametrize("segmented", [False, True])
def test_exact_candles_and_evidence(
    trade_registry: tuple[Path, tuple[str, ...]],
    replay_request: ReplayRequest,
    candles: tuple[Candle, ...],
    segmented: bool,
) -> None:
    path, ids = trade_registry
    before = {
        p: sha256(p.read_bytes()).hexdigest() for p in (path, path.parent / "synthetic-trade.jsonl")
    }
    with session(path, ids) as active:
        with PreparedTradeHistory(
            path,
            ids,
            replay_request,
            mode=MODE if segmented else VerificationMode.STRICT_FULL,
            session=active if segmented else None,
        ) as history:
            actual = tuple(history.read(SYMBOL, candles[0].open_time, END))
            approved = tuple(
                e.record
                for e in HistoricalQuery(path, ids).read_trade(
                    candles[0].open_time, END, cutoff=END
                )
            )
            assert actual == approved == candles
            evidence = history.evidence
            assert evidence.warmup_read == evidence.warmup_consecutive == 1560
            assert evidence.warmup_status == evidence.coverage_status == "AVAILABLE"
            assert evidence.authentication == "NOT_AUTHENTICATED"
            assert evidence.economic_authorization == "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
            assert evidence.historical_financial_adaptation == "NOT_AUTHORIZED"
            assert evidence.historical_execution_evidence == "NOT_AVAILABLE"
            assert evidence.provenances == ("SYNTHETIC_FIXTURE",)
            assert evidence.query_digest == evidence.reconciliation.causal_digest
            assert (
                evidence.candle_digest
                == sha256(b"".join(encoded((i, c)) for i, c in enumerate(candles, 1))).hexdigest()
            )
            assert evidence.bindings[0].registration.declaration.acquisition_time > END
            assert evidence.source_state is (
                VerificationState.SEGMENT_VERIFIED
                if segmented
                else VerificationState.FULL_SOURCE_VERIFIED
            )
            assert evidence.original_state is (
                VerificationState.SOURCE_NOT_REVALIDATED
                if segmented
                else VerificationState.FULL_SOURCE_VERIFIED
            )
            assert history._db is not None
            assert history._db.execute("PRAGMA query_only").fetchone() == (1,)
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                history._db.execute("DELETE FROM candles")
            with pytest.raises(FrozenInstanceError):
                evidence.authentication = "SOURCE_AUTHENTICATED"  # type: ignore[misc]
            with pytest.raises(FrozenInstanceError):
                actual[0].volume = Decimal("999")  # type: ignore[misc]
        assert not history.path.exists()
    assert all(sha256(p.read_bytes()).hexdigest() == value for p, value in before.items())


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("inputs", ["no_quotes", "quotes", "facts"])
def test_replay_equivalence_and_frozen_base(
    trade_registry: tuple[Path, tuple[str, ...]],
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    segmented: bool,
    inputs: str,
) -> None:
    path, ids = trade_registry
    q = NoQuotes() if inputs == "no_quotes" else quotes
    f = facts if inputs == "facts" else FixtureFacts()
    with SQLiteHistory(database) as reference:
        approved = ReplayRunner(reference, replay_request, quotes=q, facts=f)
        expected = approved.run()
        baseline = BASELINES[inputs]
        assert (
            expected.event_digest,
            sha256(encoded(expected)).hexdigest(),
            sha256(encoded(approved.snapshot.emissions)).hexdigest(),
            approved.snapshot.integrity_digest,
        ) == baseline
    with session(path, ids) as active:
        with PreparedTradeHistory(
            path,
            ids,
            replay_request,
            mode=MODE if segmented else VerificationMode.STRICT_FULL,
            session=active if segmented else None,
        ) as history:
            actual = ReplayRunner(history, replay_request, quotes=q, facts=f)
            assert actual.run() == expected
            assert actual.snapshot.emissions == approved.snapshot.emissions
            assert actual.snapshot.integrity_digest == baseline[3]
            assert actual.simulator is None
            assert actual.ledger is None
            assert actual.performance is None
            assert actual.risk is None
            if inputs == "no_quotes":
                assert isinstance(actual.quotes, NoQuotes)
                assert expected.order_intents == 0


@pytest.mark.parametrize("segmented", [False, True])
def test_continuous_fragmented_same_validated_history(
    trade_registry: tuple[Path, tuple[str, ...]],
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    segmented: bool,
) -> None:
    path, ids = trade_registry
    with session(path, ids) as active:
        with PreparedTradeHistory(
            path,
            ids,
            replay_request,
            mode=MODE if segmented else VerificationMode.STRICT_FULL,
            session=active if segmented else None,
        ) as history:
            continuous = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
            expected = continuous.run()
            runner = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
            for cutoff in (
                START - timedelta(days=1),
                START,
                T,
                DUE - timedelta(microseconds=1),
                DUE,
                DUE + timedelta(seconds=1),
                END,
            ):
                runner.run(cutoff)
                saved = runner.snapshot
                runner = ReplayRunner(
                    history, replay_request, quotes=quotes, facts=facts, snapshot=saved
                )
            assert runner.run() == expected
            assert runner.snapshot == continuous.snapshot


def test_new_preparation_cannot_inherit_replay_snapshot(
    trade_registry: tuple[Path, tuple[str, ...]],
    replay_request: ReplayRequest,
) -> None:
    path, ids = trade_registry
    with PreparedTradeHistory(path, ids, replay_request) as first:
        runner = ReplayRunner(first, replay_request)
        runner.run(START)
        with PreparedTradeHistory(path, ids, replay_request) as second:
            with pytest.raises(ValueError, match="sesión"):
                ReplayRunner(second, replay_request, snapshot=runner.snapshot)


@pytest.mark.parametrize("delta", [0, 30, 59, 60])
def test_candle_only_after_close(tmp_path: Path, delta: int) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with PreparedTradeHistory(path, ids, request) as history:
        end = LAUNCH + timedelta(seconds=delta)
        if delta == 0:
            with pytest.raises(ValueError):
                tuple(history.read(SYMBOL, LAUNCH, end))
        else:
            result = tuple(history.read(SYMBOL, LAUNCH, end))
            assert len(result) == (1 if delta == 60 else 0)
            assert all(c.close_time <= end for c in result)


def test_cutoff_preserves_unavailable_future(tmp_path: Path) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    cutoff = LAUNCH + timedelta(seconds=90)
    with PreparedTradeHistory(path, ids, request, cutoff=cutoff) as history:
        assert history.evidence.coverage_status == "INCOMPLETE"
        assert history.evidence.reconciliation.recognized == 1
        assert len(tuple(history.read(SYMBOL, LAUNCH, LAUNCH + MINUTE))) == 1
        with pytest.raises(ValueError, match="CUTOFF"):
            tuple(history.read(SYMBOL, LAUNCH, request.end))


@pytest.mark.parametrize("segmented", [False, True])
def test_launch_and_gaps_remain_incomplete(tmp_path: Path, segmented: bool) -> None:
    registration = spec(tmp_path, "gapped", [kline(), kline(2)])
    path, ids = registered(tmp_path, registration)
    request = ReplayRequest(LAUNCH, LAUNCH + 3 * MINUTE, allow_entries=True)
    with session(path, ids) as active:
        with PreparedTradeHistory(
            path,
            ids,
            request,
            mode=MODE if segmented else VerificationMode.STRICT_FULL,
            session=active if segmented else None,
        ) as history:
            assert history.start == LAUNCH
            assert (
                len(tuple(history.read(SYMBOL, request.warmup_start(AltBConfig()), request.end)))
                == 2
            )
            assert (
                history.evidence.warmup_status == history.evidence.coverage_status == "INCOMPLETE"
            )
            assert history.evidence.reconciliation.gaps
            report = ReplayRunner(history, request).run()
            assert report.signals == report.order_intents == 0
            assert report.stats.warmup_applied == 0


@pytest.mark.parametrize("missing", [0, 1000, 1559])
def test_missing_warmup_not_a_valid_signal(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    replay_request: ReplayRequest,
    missing: int,
) -> None:
    rows = candles[:missing] + candles[missing + 1 :]
    path, ids = registered(tmp_path, trade_registration(tmp_path, rows))
    with PreparedTradeHistory(path, ids, replay_request) as history:
        assert history.evidence.warmup_status == "INCOMPLETE"
        assert history.evidence.warmup_consecutive < 1560
        runner = ReplayRunner(history, replay_request)
        report = runner.run(START)
        assert report.signals == report.order_intents == 0


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("segmented", [False, True])
def test_no_arbitrary_source_priority(tmp_path: Path, conflict: bool, segmented: bool) -> None:
    first = spec(tmp_path, "monthly", [kline(i) for i in range(3)])
    rows = [kline(i) for i in range(3)]
    if conflict:
        rows[1][4] = "102"
    second = spec(tmp_path, "daily", rows)
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    ids = (registry.register(first).dataset_id, registry.register(second).dataset_id)
    request = ReplayRequest(LAUNCH, LAUNCH + 3 * MINUTE)
    with session(registry.path, ids) as active:
        if conflict:
            with pytest.raises(ValueError):
                PreparedTradeHistory(
                    registry.path,
                    ids,
                    request,
                    mode=MODE if segmented else VerificationMode.STRICT_FULL,
                    session=active if segmented else None,
                )
        else:
            with PreparedTradeHistory(
                registry.path,
                ids,
                request,
                mode=MODE if segmented else VerificationMode.STRICT_FULL,
                session=active if segmented else None,
            ) as history:
                assert history.evidence.reconciliation.recognized == 3
                assert history.evidence.reconciliation.duplicates == 3
                assert len(tuple(history.read(SYMBOL, LAUNCH, request.end))) == 3
                assert tuple(b.dataset_id for b in history.evidence.bindings) == tuple(sorted(ids))


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("during", [False, True])
def test_source_change_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segmented: bool,
    during: bool,
) -> None:
    path, ids, request, registration = small_registry(tmp_path)
    source = Path(registration.original_file)
    original = PreparedTradeHistory._stage

    def changed(
        self: PreparedTradeHistory, stream: Iterator[HistoricalEvent], limits: AdapterLimits
    ) -> tuple[str, str]:
        def changing() -> Iterator[HistoricalEvent]:
            yield next(stream)
            source.write_text(source.read_text().replace('"101"', '"102"'))
            yield from stream

        return original(self, changing(), limits)

    if not during:
        source.write_text(source.read_text().replace('"101"', '"102"'))
    else:
        monkeypatch.setattr(PreparedTradeHistory, "_stage", changed)
    with pytest.raises((ValueError, OSError)):
        if segmented:
            with session(path, ids) as active:
                PreparedTradeHistory(path, ids, request, mode=MODE, session=active)
        else:
            PreparedTradeHistory(path, ids, request)


@pytest.mark.parametrize("state", ["PROVISIONAL", "FAILED", "INTERRUPTED", "bad_digest"])
def test_unfinished_or_digest_mismatch_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    original = QueryStream.__next__
    created: list[Path] = []
    stage = PreparedTradeHistory._stage

    def tracking(
        self: PreparedTradeHistory, stream: Iterator[HistoricalEvent], limits: AdapterLimits
    ) -> tuple[str, str]:
        created.append(self.path.parent)
        return stage(self, stream, limits)

    def incomplete(self: QueryStream[Any]) -> Any:
        try:
            return original(self)
        except StopIteration:
            if state == "bad_digest":
                self._economic.update(b"forged")
            else:
                self.state = state
            raise

    monkeypatch.setattr(QueryStream, "__next__", incomplete)
    monkeypatch.setattr(PreparedTradeHistory, "_stage", tracking)
    with session(path, ids) as active:
        with pytest.raises(ValueError, match="QUERY_STREAM"):
            PreparedTradeHistory(path, ids, request, mode=MODE, session=active)
    assert created and all(not p.exists() for p in created)


@pytest.mark.parametrize("field", ["event_id", "payload", "links"])
def test_segment_cache_tamper_blocks_adapter(tmp_path: Path, field: str) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with session(path, ids) as active:
        with sqlite3.connect(active.cache_path) as db:
            if field == "links":
                db.execute("DELETE FROM links WHERE sequence=2")
            else:
                db.execute(f"UPDATE locators SET {field}='forged' WHERE sequence=2")
        with pytest.raises((ValueError, json.JSONDecodeError)):
            PreparedTradeHistory(path, ids, request, mode=MODE, session=active)


@pytest.mark.parametrize("field", ["records", "segments", "schema", "audit"])
def test_registry_tamper_blocks_adapter(tmp_path: Path, field: str) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with sqlite3.connect(path) as db:
        if field == "records":
            db.execute("UPDATE records SET event_id='forged'")
        elif field == "segments":
            db.execute("UPDATE segments SET digest='forged'")
        elif field == "audit":
            db.execute("UPDATE datasets SET audit='{}'")
        else:
            value = json.loads(db.execute("SELECT spec FROM datasets").fetchone()[0])
            value["schema_version"] = 2
            db.execute("UPDATE datasets SET spec=?", (json.dumps(value),))
    with pytest.raises(ValueError):
        PreparedTradeHistory(path, ids, request)


@pytest.mark.parametrize(
    "kind", [DatasetType.MARK, DatasetType.FUNDING, DatasetType.BRACKETS, DatasetType.FEES]
)
def test_non_trade_not_authorized(tmp_path: Path, kind: DatasetType) -> None:
    rows: list[object] = [kline()] if kind is DatasetType.MARK else [rule(kind)]
    # Una selección con otra categoría nunca se adapta, incluso antes de leer sus registros.
    registration = spec(tmp_path, "not-trade", rows, kind)
    if kind is DatasetType.FUNDING:
        from .test_historical_registry import funding

        registration = spec(tmp_path, "not-trade", [funding()], kind)
    path, ids = registered(tmp_path, registration)
    with pytest.raises(ValueError, match="TRADE_DATASETS_REQUIRED"):
        PreparedTradeHistory(path, ids, ReplayRequest(LAUNCH, LAUNCH + 3 * MINUTE))


@pytest.mark.parametrize("column", ["payload", "opened", "sequence", "delete"])
def test_prepared_cache_tamper_against_protected_roots(tmp_path: Path, column: str) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with PreparedTradeHistory(path, ids, request) as history:
        assert history._db is not None
        # Atacante escribe por otra conexión; conserva las raíces confiables de proceso.
        history._db.close()
        with sqlite3.connect(history.path) as writer:
            if column == "delete":
                writer.execute("DELETE FROM candles WHERE sequence=2")
            elif column == "payload":
                text = writer.execute("SELECT payload FROM candles WHERE sequence=2").fetchone()[0]
                writer.execute(
                    "UPDATE candles SET payload=? WHERE sequence=2",
                    (text.replace('"101"', '"102"'),),
                )
            else:
                writer.execute(
                    f"UPDATE candles SET {column}=? WHERE sequence=2",
                    (999 if column == "sequence" else 0,),
                )
        history._open_snapshot()
        with pytest.raises((ValueError, KeyError)):
            tuple(history.read(SYMBOL, LAUNCH, request.end))
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            tuple(history.read(SYMBOL, LAUNCH, request.end))


def test_closed_or_expired_session_blocks_consumption(tmp_path: Path) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with session(path, ids) as active:
        with PreparedTradeHistory(path, ids, request, mode=MODE, session=active) as history:
            active._started -= active.policy.max_seconds + 1
            with pytest.raises(ValueError):
                tuple(history.read(SYMBOL, LAUNCH, request.end))
    with PreparedTradeHistory(path, ids, request) as history:
        history.close()
        history.close()
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            tuple(history.read(SYMBOL, LAUNCH, request.end))


@pytest.mark.parametrize("quota", ["rows", "disk"])
def test_bounded_preparation_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candles: tuple[Candle, ...],
    replay_request: ReplayRequest,
    quota: str,
) -> None:
    path, ids = registered(tmp_path, trade_registration(tmp_path, candles))
    created: list[Path] = []
    stage = PreparedTradeHistory._stage

    def tracking(
        self: PreparedTradeHistory, stream: Iterator[HistoricalEvent], limits: AdapterLimits
    ) -> tuple[str, str]:
        created.append(self.path.parent)
        return stage(self, stream, limits)

    monkeypatch.setattr(PreparedTradeHistory, "_stage", tracking)
    limits = (
        AdapterLimits(max_rows=1) if quota == "rows" else AdapterLimits(max_temporary_bytes=16384)
    )
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        PreparedTradeHistory(path, ids, replay_request, limits=limits)
    assert all(not p.exists() for p in created)


@pytest.mark.parametrize("value", [True, False, "100", 0, -1, 1_000_001])
def test_limits_reject_invalid_rows(value: Any) -> None:
    with pytest.raises(ValueError):
        AdapterLimits(max_rows=value)


def test_explicit_mode_contract_and_scope(tmp_path: Path) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with pytest.raises(ValueError):
        PreparedTradeHistory(path, (), request)
    with pytest.raises(ValueError, match="sesión explícita"):
        PreparedTradeHistory(path, ids, request, mode=MODE)
    with session(path, ids) as active:
        with pytest.raises(ValueError, match="no reutiliza"):
            PreparedTradeHistory(path, ids, request, session=active)
    with PreparedTradeHistory(path, ids, request) as history:
        with pytest.raises(ValueError, match="CONTRACT_MISMATCH"):
            tuple(history.read(Symbol("SHIBUSDC"), LAUNCH, request.end))
        with pytest.raises(ValueError, match="OUTSIDE"):
            tuple(history.read(SYMBOL, LAUNCH, request.end + MINUTE))


def test_hostile_decimal_no_network_no_original_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket, "socket", blocked)
    path, ids, request, registration = small_registry(tmp_path)
    source = Path(registration.original_file)
    initial = source.read_bytes(), path.read_bytes()
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        with PreparedTradeHistory(path, ids, request) as history:
            values = tuple(history.read(SYMBOL, LAUNCH, request.end))
            assert values[0].close == Price(Decimal("101"))
            assert values[0].volume == Decimal("10")
            assert values[0].taker_buy_base_volume == Decimal("5")
            assert ReplayRunner(history, request).run().order_intents == 0
    assert (source.read_bytes(), path.read_bytes()) == initial
