"""Fixtures SYNTHETIC_FIXTURE; nunca datos Binance reales ni autorización económica."""

import csv
import json
import socket
import sqlite3
import zipfile
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from trading_engine.binance.klines import CSV_HEADER, DEFAULT_SYMBOL, LAUNCH, to_ms
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source
from trading_engine.replay.encoding import encoded
from trading_engine.replay.historical_validation.audit import FundingInventory
from trading_engine.replay.historical_validation.contracts import (
    Contract,
    DatasetDeclaration,
    DatasetType,
    Limits,
    units,
)
from trading_engine.replay.historical_validation.parsers import MarkKline
from trading_engine.replay.historical_validation.query import (
    BlockedHistoricalInterval,
    HistoricalQuery,
)
from trading_engine.replay.historical_validation.readiness import registry_readiness
from trading_engine.replay.historical_validation.reconciliation import HistoricalReconciliation
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.segments import (
    Registration,
    RegistryProvenance,
    SegmentBounds,
)
from trading_engine.replay.historical_validation.sqlite_audit import audit_sqlite
from trading_engine.replay.historical_validation.storage import connection

T = LAUNCH
END = T + timedelta(minutes=3)
VALIDATED = END + timedelta(days=2)


def kline(index: int = 0) -> list[object]:
    opened = to_ms(T + timedelta(minutes=index))
    return [opened, "100", "103", "99", "101", "10", opened + 59999, "1000", 2, "5", "500", "0"]


def funding(index: int = 0) -> dict[str, object]:
    at = T + timedelta(minutes=index)
    return {
        "symbol": "1000SHIBUSDC",
        "fundingTime": to_ms(at),
        "fundingRate": "0.0001",
        "markPrice": "100",
        "market_available_at": at.isoformat(),
    }


def rule(kind: DatasetType) -> dict[str, object]:
    data: dict[str, object] = {
        "symbol": "1000SHIBUSDC",
        "margin_asset": "USDC",
        "valid_from": T.isoformat(),
        "valid_until": END.isoformat(),
        "market_available_at": T.isoformat(),
        "source": "SYNTHETIC_FIXTURE",
        "source_version": "1",
        "quality": "VERIFIED_FOR_PERIOD",
    }
    if kind is DatasetType.BRACKETS:
        data["brackets"] = [
            {
                "notional_floor": "0",
                "notional_cap": "10000",
                "maintenance_rate": "0.01",
                "maintenance_amount": "0",
                "max_leverage": "4",
            }
        ]
    elif kind is DatasetType.FEES:
        data.update(
            maker_rate="0.0001",
            taker_rate="0.0004",
            user_tier="fixture",
            promotion="NONE_DECLARED",
            discount="0",
            fee_currency="USDC",
            effective_role_evidence="UNKNOWN",
        )
    else:
        data.update(
            tick_size="0.001",
            step_size="1",
            min_quantity="1",
            max_quantity="1000",
            min_notional="5",
            min_price="0.001",
            max_price="1000",
            max_leverage="4",
            contract_status="TRADING",
        )
    return data


def spec(
    tmp_path: Path,
    name: str,
    rows: Sequence[object],
    kind: DatasetType = DatasetType.TRADE,
    *,
    start: datetime = T,
    end: datetime = END,
    file_format: str = "jsonl",
) -> Registration:
    path = tmp_path / (name + "." + file_format)
    if file_format == "csv":
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(CSV_HEADER)
            writer.writerows(rows)  # type: ignore[arg-type]
    elif file_format == "json":
        path.write_text(json.dumps(rows))
    else:
        path.write_text("\n".join(json.dumps(row) for row in rows))
    declaration = DatasetDeclaration(
        name,
        kind,
        Contract(),
        "1m" if kind in {DatasetType.TRADE, DatasetType.MARK} else "EVENTS",
        start,
        end,
        "local:" + name,
        "SYNTHETIC_FIXTURE_NOT_OFFICIAL",
        END + timedelta(days=1),
        "historical-local-v1",
        units(kind),
    )
    return Registration(
        declaration,
        str(path),
        file_format,
        VALIDATED,
        RegistryProvenance.FIXTURE,
        "Synthetic fixture generated locally",
        (SegmentBounds(start, end),),
    )


def query(
    tmp_path: Path, *specs: Registration
) -> tuple[HistoricalDatasetRegistry, HistoricalQuery]:
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    ids = tuple(registry.register(s).dataset_id for s in specs)
    return registry, HistoricalQuery(registry.path, ids)


@pytest.mark.parametrize("kind", [DatasetType.TRADE, DatasetType.MARK])
@pytest.mark.parametrize("file_format", ["csv", "jsonl", "json"])
def test_register_roundtrip_original_and_idempotence(
    tmp_path: Path, kind: DatasetType, file_format: str
) -> None:
    s = spec(tmp_path, "monthly", [kline(i) for i in range(3)], kind, file_format=file_format)
    before = Path(s.original_file).read_bytes()
    registry, q = query(tmp_path, s)
    first = registry.verify(q.dataset_ids[0])
    assert registry.register(s) == first
    assert first.authentication == "NOT_AUTHENTICATED"
    assert first.row_count == 3
    assert first.segment_ids
    assert Path(s.original_file).read_bytes() == before
    assert len(list(q.read(kind, T, END, cutoff=END))) == 3
    with connection(registry.path) as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM records").fetchone() == (3,)
        assert (
            db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name IN ('futures_klines_1m','import_manifest')"
            ).fetchall()
            == []
        )
        if kind is DatasetType.TRADE:
            assert db.execute(
                "SELECT count(*) FROM records WHERE payload IS NOT NULL"
            ).fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM records")


@pytest.mark.parametrize("case", ["complete", "gap", "complement", "overlap", "numeric"])
def test_month_daily_consolidation(tmp_path: Path, case: str) -> None:
    monthly = (
        [kline(0), kline(2)] if case in {"gap", "complement"} else [kline(i) for i in range(3)]
    )
    a = spec(tmp_path, "monthly", monthly)
    specs = [a]
    if case in {"complement", "overlap", "numeric"}:
        daily = kline(1)
        if case == "numeric":
            daily[1] = "100.000000"
        specs.append(spec(tmp_path, "daily", [daily]))
    _, q = query(tmp_path, *specs)
    report = HistoricalReconciliation(q).evaluate(DatasetType.TRADE, T, END, cutoff=END)
    assert report.recognized == (2 if case == "gap" else 3)
    assert report.coverage_status == ("PARTIAL" if case == "gap" else "AVAILABLE_FOR_REVIEW")
    assert report.duplicates == (1 if case in {"overlap", "numeric"} else 0)
    assert bool(report.gaps) == (case == "gap")


@pytest.mark.parametrize(
    "field,value",
    [(1, "100.1"), (4, "102"), (5, "11"), (7, "1001"), (8, 3), (9, "6"), (10, "501"), (11, "1")],
)
def test_cross_source_conflicts_block_and_are_persisted(
    tmp_path: Path, field: int, value: object
) -> None:
    changed = kline(1)
    changed[field] = value
    registry, q = query(
        tmp_path, spec(tmp_path, "a", [kline(i) for i in range(3)]), spec(tmp_path, "b", [changed])
    )
    reconciler = HistoricalReconciliation(q)
    report = reconciler.evaluate(DatasetType.TRADE, T, END, cutoff=END)
    assert report.coverage_status == "CONFLICTED"
    assert report.conflicts[0].reason == "CONFLICTED_SAME_TIMESTAMP"
    assert len(report.conflicts[0].dataset_ids) == 2
    assert reconciler.persist(report) == reconciler.persist(report)
    with connection(registry.path) as db:
        assert db.execute("SELECT count(*) FROM conflicts").fetchone() == (1,)
    with pytest.raises(BlockedHistoricalInterval):
        next(q.read_trade(T, END, cutoff=END))
    # El conflicto posterior no bloquea el prefijo anterior a su disponibilidad.
    assert len(list(q.read_trade(T, END, cutoff=T + timedelta(minutes=1)))) == 1


@pytest.mark.parametrize(
    "kind",
    [
        DatasetType.TRADE,
        DatasetType.MARK,
        DatasetType.FUNDING,
        DatasetType.BRACKETS,
        DatasetType.FEES,
        DatasetType.SYMBOL_RULES,
    ],
)
def test_future_dataset_preserves_causal_prefix(tmp_path: Path, kind: DatasetType) -> None:
    cutoff = T + timedelta(minutes=1)
    prefix: list[object]
    full: list[object]
    if kind in {DatasetType.TRADE, DatasetType.MARK}:
        prefix, full = [kline(0)], [kline(i) for i in range(3)]
    elif kind is DatasetType.FUNDING:
        prefix, full = [funding(0), funding(1)], [funding(i) for i in range(3)]
    else:
        initial = rule(kind)
        initial["valid_until"] = cutoff.isoformat()
        later = rule(kind)
        later["valid_from"] = (T + timedelta(minutes=2)).isoformat()
        later["market_available_at"] = later["valid_from"]
        prefix, full = [initial], [initial, later]
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    a = registry.register(spec(tmp_path, "a", prefix, kind))
    b = registry.register(spec(tmp_path, "b", full, kind))
    assert a.source_hash != b.source_hash
    qa, qb = (
        HistoricalQuery(registry.path, (a.dataset_id,)),
        HistoricalQuery(registry.path, (b.dataset_id,)),
    )
    a_report = HistoricalReconciliation(qa).evaluate(kind, T, END, cutoff=cutoff)
    b_report = HistoricalReconciliation(qb).evaluate(kind, T, END, cutoff=cutoff)
    assert a_report.prefix_key() == b_report.prefix_key()
    assert [e.economic_key() for e in qa.read(kind, T, END, cutoff=cutoff)] == [
        e.economic_key() for e in qb.read(kind, T, END, cutoff=cutoff)
    ]


def test_mark_exact_close_and_funding_publication(tmp_path: Path) -> None:
    rate = funding(0)
    rate["market_available_at"] = (T + timedelta(minutes=2)).isoformat()
    _, q = query(
        tmp_path,
        spec(tmp_path, "mark", [kline(0)], DatasetType.MARK),
        spec(tmp_path, "funding", [rate], DatasetType.FUNDING),
    )
    assert list(q.read_mark(T, END, cutoff=T + timedelta(seconds=59))) == []
    event = list(q.read_mark(T, END, cutoff=T + timedelta(minutes=1)))[0]
    assert isinstance(event.record, MarkKline)
    assert event.record.close_sample(T + timedelta(minutes=1)) == Decimal("101")
    with pytest.raises(ValueError):
        event.record.close_sample(T)
    assert list(q.read_funding(T, END, cutoff=T + timedelta(minutes=1))) == []
    assert len(list(q.read_funding(T, END, cutoff=T + timedelta(minutes=2)))) == 1
    assert list(q.read_funding(T + timedelta(minutes=1), END, cutoff=END)) == []


@pytest.mark.parametrize("inventory", [True, False])
def test_funding_requires_declared_inventory(tmp_path: Path, inventory: bool) -> None:
    rates = [funding(0), funding(2)]
    _, q = query(tmp_path, spec(tmp_path, "funding", rates, DatasetType.FUNDING))
    inv = (
        FundingInventory(T, END, (T, T + timedelta(minutes=2)), "SYNTHETIC_FIXTURE", END)
        if inventory
        else None
    )
    r = HistoricalReconciliation(q).evaluate(
        DatasetType.FUNDING, T, END, cutoff=END, funding_inventory=inv
    )
    assert r.coverage_status == ("AVAILABLE_FOR_REVIEW" if inventory else "PARTIAL")
    assert r.funding_status != "NOT_APPLICABLE"


@pytest.mark.parametrize("kind", [DatasetType.BRACKETS, DatasetType.FEES, DatasetType.SYMBOL_RULES])
def test_overlapping_rule_validities_block(tmp_path: Path, kind: DatasetType) -> None:
    later = rule(kind)
    later["valid_from"] = (T + timedelta(minutes=1)).isoformat()
    later["market_available_at"] = later["valid_from"]
    _, q = query(
        tmp_path, spec(tmp_path, "a", [rule(kind)], kind), spec(tmp_path, "b", [later], kind)
    )
    with pytest.raises(BlockedHistoricalInterval, match="CONFLICTED_RULE_VALIDITY"):
        next(q.read_rules(kind, T, END, cutoff=END))
    assert len(list(q.read_rules(kind, T, END, cutoff=T))) == 1


@pytest.mark.parametrize("quality", ["CURRENT_ONLY", "UNKNOWN_HISTORY", "DOCUMENTED_AT_LAUNCH"])
def test_fee_quality_never_grants_historical_use(tmp_path: Path, quality: str) -> None:
    fee = rule(DatasetType.FEES)
    fee["quality"] = quality
    _, q = query(tmp_path, spec(tmp_path, "fees", [fee], DatasetType.FEES))
    with pytest.raises(ValueError, match="NOT_CAUSALLY_USABLE"):
        list(q.read_rules(DatasetType.FEES, T, END, cutoff=END))
    ready = registry_readiness(HistoricalReconciliation(q), T, END, cutoff=END)
    assert ready.historical_fees == "NOT_CAUSALLY_USABLE"
    assert ready.trade_completeness == "MISSING"


@pytest.mark.parametrize("failure", ["callback", "changed", "checksum"])
def test_atomic_rollback_and_recovery(tmp_path: Path, failure: str) -> None:
    s = spec(tmp_path, "a", [kline(i) for i in range(3)])
    original = Path(s.original_file).read_bytes()
    checksum = tmp_path / "checksum"
    checksum.write_text(sha256(original).hexdigest() + "  " + Path(s.original_file).name + "\n")
    s = replace(s, checksum_file=str(checksum))
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")

    def interrupt() -> None:
        if failure == "callback":
            raise RuntimeError("SYNTHETIC_COMMIT_FAILURE")
        if failure == "changed":
            Path(s.original_file).write_bytes(original + b"\n")
        else:
            checksum.write_text("0" * 64 + "  " + Path(s.original_file).name + "\n")

    with pytest.raises((RuntimeError, ValueError)):
        registry.register(s, before_commit=interrupt)
    with connection(registry.path) as db:
        for table in ("datasets", "records", "segments"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert db.execute("SELECT state FROM imports").fetchone() == ("FAILED",)
    Path(s.original_file).write_bytes(original)
    checksum.write_text(sha256(original).hexdigest() + "  " + Path(s.original_file).name + "\n")
    first = registry.register(s)
    assert registry.register(s) == first
    assert registry.verify(first.dataset_id) == first
    with connection(registry.path) as db:
        assert db.execute("SELECT state FROM import_events ORDER BY state").fetchall() == [
            ("COMPLETE_IMPORT",),
            ("FAILED",),
        ]


@pytest.mark.parametrize(
    "change", ["source", "payload", "event_id", "segment", "audit", "extra_record", "identity"]
)
def test_tampered_registry_rejected_even_with_recomputed_digest(
    tmp_path: Path, change: str
) -> None:
    s = spec(tmp_path, "mark", [kline(i) for i in range(3)], DatasetType.MARK)
    registry, q = query(tmp_path, s)
    if change == "source":
        Path(s.original_file).write_text("[]")
    else:
        with connection(registry.path, read_only=False) as db:
            if change == "payload":
                db.execute("UPDATE records SET payload='[]'")
            elif change == "event_id":
                db.execute("UPDATE records SET event_id=?", ("f" * 64,))
            elif change == "segment":
                db.execute("UPDATE segments SET digest=?,row_count=999", ("f" * 64,))
            elif change == "audit":
                db.execute(
                    "UPDATE datasets SET audit='{}',audit_id=?", (sha256(b"{}").hexdigest(),)
                )
            elif change == "identity":
                db.execute("UPDATE datasets SET spec=replace(spec,'local:mark','local:other')")
            else:
                db.execute(
                    "INSERT INTO records SELECT dataset_id,sequence+10,effective,available,"
                    "valid_until,event_id,raw_digest,payload FROM records WHERE sequence=1"
                )
    with pytest.raises(ValueError):
        registry.verify(q.dataset_ids[0])
    with pytest.raises(ValueError):
        list(q.read_mark(T, END, cutoff=END))


@pytest.mark.parametrize(
    "invalid", ["ohlc", "taker", "timestamp", "infinite", "out_of_period", "order", "malformed"]
)
def test_invalid_sources_fail_audibly(tmp_path: Path, invalid: str) -> None:
    rows: list[object] = [kline(i) for i in range(3)]
    changed = kline(0)
    if invalid == "ohlc":
        changed[2] = "1"
    elif invalid == "taker":
        changed[9] = "11"
    elif invalid == "timestamp":
        changed[0] = to_ms(T) - 60000
    elif invalid == "infinite":
        changed[1] = "Infinity"
    elif invalid == "out_of_period":
        changed = kline(3)
    elif invalid == "malformed":
        changed = []
    rows[0] = changed
    if invalid == "order":
        rows.reverse()
    s = spec(tmp_path, "bad", rows)
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    with pytest.raises((ValueError, KeyError, TypeError)):
        registry.register(s)
    with connection(registry.path) as db:
        assert db.execute("SELECT state FROM imports").fetchone() == ("FAILED",)
        assert db.execute("SELECT count(*) FROM datasets").fetchone() == (0,)


@pytest.mark.parametrize("invalid", ["USDT", "spot", "symbol", "units", "auth"])
def test_wrong_contract_and_forged_authentication(tmp_path: Path, invalid: str) -> None:
    if invalid == "auth":
        item = rule(DatasetType.FEES)
        item["authentication_status"] = "SOURCE_AUTHENTICATED"
        s = spec(tmp_path, "bad", [item], DatasetType.FEES)
        with pytest.raises(ValueError):
            query(tmp_path, s)
    elif invalid == "units":
        s = spec(tmp_path, "bad", [kline()])
        with pytest.raises(ValueError):
            replace(s.declaration, unit_contract="quantity=SHIB")
    else:
        from trading_engine.models.values import Symbol

        with pytest.raises(ValueError):
            Contract(
                symbol=Symbol("1000SHIBUSDT") if invalid == "symbol" else DEFAULT_SYMBOL,
                market="SPOT" if invalid == "spot" else "BINANCE_USDM_FUTURES",
                settlement_asset="USDT" if invalid == "USDT" else "USDC",
            )


@pytest.mark.parametrize("attack", ["checksum", "path", "bomb", "corrupt", "multiple", "limits"])
def test_archive_guards_reused(tmp_path: Path, attack: str) -> None:
    s = spec(tmp_path, "a", [kline()])
    if attack == "checksum":
        checksum = tmp_path / "checksum"
        checksum.write_text("0" * 64 + "  " + Path(s.original_file).name + "\n")
        s = replace(s, checksum_file=str(checksum))
    elif attack == "limits":
        s = replace(s, limits=Limits(max_file_bytes=1))
    else:
        archive = tmp_path / "archive.zip"
        member = "../evil.csv" if attack == "path" else "a.jsonl"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                member, "0" * 100000 if attack == "bomb" else Path(s.original_file).read_text()
            )
            if attack == "multiple":
                z.writestr("unexpected", "x")
        if attack == "corrupt":
            archive.write_bytes(b"not a zip")
        s = replace(s, original_file=str(archive), member=member)
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        query(tmp_path, s)


def test_sqlite_source_byte_preserved_and_not_causally_authorized(tmp_path: Path) -> None:
    from trading_engine.binance.klines import normalize_kline

    source = tmp_path / "production.sqlite3"
    with FuturesSQLite(source) as db:
        db.import_candles(
            (normalize_kline(kline(i), DEFAULT_SYMBOL, as_of=END) for i in range(3)),
            symbol=DEFAULT_SYMBOL,
            file="SYNTHETIC",
            period="fixture",
            source=Source.MONTHLY,
            as_of=END,
            checksum="0" * 64,
        )
    before = source.read_bytes()
    s = spec(tmp_path, "declared", [kline()])
    result = audit_sqlite(source, s.declaration, as_of=END)
    assert result.causal_status == "BLOCKED_REQUIRES_SEPARATE_PREFIX_MANIFEST_REVIEW"
    assert source.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        HistoricalDatasetRegistry(source)
    assert source.read_bytes() == before


def test_decimal_hostile_context_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket, "socket", blocked)
    a = spec(tmp_path, "a", [kline(i) for i in range(3)])
    same = kline(0)
    same[1] = "100.00000000000000000001"
    b = spec(tmp_path, "b", [same])
    _, q = query(tmp_path, a, b)
    normal = HistoricalReconciliation(q).evaluate(DatasetType.TRADE, T, END, cutoff=END)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        assert HistoricalReconciliation(q).evaluate(DatasetType.TRADE, T, END, cutoff=END) == normal
    assert normal.conflicts


def test_missing_readiness_precontract_and_incremental_matrix(tmp_path: Path) -> None:
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    q = HistoricalQuery(registry.path, ())
    r = registry_readiness(HistoricalReconciliation(q), T, END, cutoff=END)
    assert r.datasets == "DATASET_NOT_PROVIDED"
    assert r.historical_brackets == "MISSING"
    assert r.financial_adaptation == "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
    assert r.liquidation_validation == "INCOMPLETE"
    matrix = HistoricalReconciliation(q).coverage_matrix(T - timedelta(days=30), END, cutoff=END)
    assert any(m.coverage.utilization == "PRE_CONTRACT" for m in matrix)
    with pytest.raises(FrozenInstanceError):
        r.trade_completeness = "READY"  # type: ignore[misc]


def test_segments_revision_and_restart(tmp_path: Path) -> None:
    s = spec(tmp_path, "a", [kline(i) for i in range(3)])
    s = replace(
        s,
        segments=(
            SegmentBounds(T, T + timedelta(minutes=1)),
            SegmentBounds(T + timedelta(minutes=1), END),
        ),
    )
    registry, q = query(tmp_path, s)
    before = encoded(list_events(q))
    restarted = HistoricalDatasetRegistry(registry.path)
    assert (
        restarted.verify(q.dataset_ids[0]).segment_ids
        == registry.verify(q.dataset_ids[0]).segment_ids
    )
    new = spec(tmp_path, "b", [kline(i) for i in range(3)])
    revised = restarted.register(new)
    assert revised.dataset_id != q.dataset_ids[0]
    assert encoded(list_events(q)) == before
    with connection(registry.path) as db:
        assert db.execute("SELECT count(*) FROM segments").fetchone() == (3,)
    with pytest.raises(ValueError):
        replace(s, segments=(SegmentBounds(T, T + timedelta(minutes=1)),))


def list_events(q: HistoricalQuery) -> tuple[Any, ...]:
    return tuple(q.read_trade(T, END, cutoff=END))


def test_registry_corruption_explicit(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite3"
    path.write_bytes(b"CORRUPTED")
    with pytest.raises(sqlite3.DatabaseError):
        HistoricalDatasetRegistry(path)


def test_actual_sqlite_commit_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    s = spec(tmp_path, "a", [kline()])
    original_connect = sqlite3.connect
    fail_once = True

    class FailingCommit(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            nonlocal fail_once
            if sql == "COMMIT" and fail_once:
                fail_once = False
                raise sqlite3.OperationalError("SYNTHETIC_COMMIT_INTERRUPTION")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=FailingCommit)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="COMMIT_INTERRUPTION"):
        registry.register(s)
    with connection(registry.path) as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone() == (0,)
        assert db.execute("SELECT state FROM imports").fetchone() == ("FAILED",)
    assert registry.verify(registry.register(s).dataset_id).row_count == 1


def test_source_change_during_stream_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_engine.replay.historical_validation import registry as module
    from trading_engine.replay.historical_validation.local_files import local_rows as original_rows

    s = spec(tmp_path, "a", [kline(i) for i in range(3)])
    modified = False

    def rows(*args: Any, **kwargs: Any) -> Any:
        nonlocal modified
        for row in original_rows(*args, **kwargs):
            yield row
            if not modified:
                modified = True
                with Path(s.original_file).open("a") as stream:
                    stream.write("\n")

    monkeypatch.setattr(module, "local_rows", rows)
    with pytest.raises(ValueError, match="cambió"):
        query(tmp_path, s)


def test_register_existing_trade_sqlite_inventory_is_read_only(tmp_path: Path) -> None:
    from trading_engine.binance.klines import normalize_kline

    path = tmp_path / "external.sqlite3"
    with FuturesSQLite(path) as store:
        store.import_candles(
            (normalize_kline(kline(i), DEFAULT_SYMBOL, as_of=END) for i in range(3)),
            symbol=DEFAULT_SYMBOL,
            file="SYNTHETIC",
            period="fixture",
            source=Source.MONTHLY,
            as_of=END,
            checksum="0" * 64,
        )
    # Sellado del fixture antes de congelarlo; el registro jamás cambia este modo.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    before = path.read_bytes()
    declaration = spec(tmp_path, "declared", [kline()]).declaration
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    identity = registry.register_trade_sqlite_inventory(
        path,
        declaration,
        validation_time=VALIDATED,
        provenance=RegistryProvenance.FIXTURE,
        provenance_evidence="SYNTHETIC_FIXTURE",
    )
    assert (
        registry.register_trade_sqlite_inventory(
            path,
            declaration,
            validation_time=VALIDATED,
            provenance=RegistryProvenance.FIXTURE,
            provenance_evidence="SYNTHETIC_FIXTURE",
        )
        == identity
    )
    assert path.read_bytes() == before
    with connection(registry.path) as db:
        audit = json.loads(db.execute("SELECT audit FROM external_trade_inventories").fetchone()[0])
        assert audit["causal_status"] == "BLOCKED_REQUIRES_SEPARATE_PREFIX_MANIFEST_REVIEW"
        assert db.execute("SELECT count(*) FROM records").fetchone() == (0,)
    with pytest.raises(ValueError, match="DATASET_NOT_PROVIDED"):
        list(HistoricalQuery(registry.path, (identity,)).read_trade(T, END, cutoff=END))


@pytest.mark.parametrize("missing", ["symbol", "fundingTime", "fundingRate", "market_available_at"])
def test_missing_funding_not_zero(tmp_path: Path, missing: str) -> None:
    rate = funding()
    del rate[missing]
    with pytest.raises((ValueError, KeyError)):
        query(tmp_path, spec(tmp_path, "bad", [rate], DatasetType.FUNDING))


def test_checksum_matrix_and_funding_inventory(tmp_path: Path) -> None:
    trade = spec(tmp_path, "trade", [kline(i) for i in range(3)])
    checksum = tmp_path / "checksum"
    checksum.write_text(
        sha256(Path(trade.original_file).read_bytes()).hexdigest()
        + "  "
        + Path(trade.original_file).name
        + "\n"
    )
    trade = replace(trade, checksum_file=str(checksum))
    rate = spec(tmp_path, "rate", [funding()], DatasetType.FUNDING)
    _, q = query(tmp_path, trade, rate)
    inventory = FundingInventory(T, END, (T,), "SYNTHETIC_FIXTURE", END)
    r = HistoricalReconciliation(q)
    matrix = r.coverage_matrix(T, END, cutoff=END, funding_inventories=(inventory,))
    trade_month = next(m for m in matrix if m.coverage.dataset_type is DatasetType.TRADE)
    assert trade_month.checksum_states == ("MATCH",)
    assert trade_month.coverage.integrity == "INTEGRITY_VALIDATED"
    assert trade_month.coverage.coverage_percent == 100
    assert trade_month.sources == ("local:trade",)
    assert trade_month.economic_authorization == "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
    ready = registry_readiness(r, T, END, cutoff=END, funding_inventory=inventory)
    assert ready.funding_completeness == "AVAILABLE_FOR_REVIEW"
    report = r.evaluate(DatasetType.FUNDING, T, END, cutoff=END, funding_inventory=inventory)
    assert r.persist(report, funding_inventory=inventory)
    with pytest.raises(ValueError, match="NOT_REPRODUCIBLE"):
        r.persist(replace(report, recognized=999), funding_inventory=inventory)


def test_segment_local_gap_status(tmp_path: Path) -> None:
    split = T + timedelta(minutes=1)
    s = replace(
        spec(tmp_path, "a", [kline(0)]),
        segments=(SegmentBounds(T, split), SegmentBounds(split, END)),
    )
    registry, q = query(tmp_path, s)
    with connection(registry.path) as db:
        assert db.execute("SELECT coverage FROM segments ORDER BY start").fetchall() == [
            ("AVAILABLE",),
            ("MISSING",),
        ]
    registry.verify(q.dataset_ids[0])


def test_noncausal_category_does_not_stop_independent_audit(tmp_path: Path) -> None:
    fee = rule(DatasetType.FEES)
    fee["quality"] = "CURRENT_ONLY"
    _, q = query(
        tmp_path,
        spec(tmp_path, "fees", [fee], DatasetType.FEES),
        spec(tmp_path, "trade", [kline(i) for i in range(3)]),
    )
    ready = registry_readiness(HistoricalReconciliation(q), T, END, cutoff=END)
    assert ready.trade_completeness == "AVAILABLE_FOR_REVIEW"
    assert ready.historical_fees == "NOT_CAUSALLY_USABLE"


def test_rule_expired_missing_brackets(tmp_path: Path) -> None:
    bracket = rule(DatasetType.BRACKETS)
    bracket["valid_until"] = (T + timedelta(minutes=1)).isoformat()
    _, q = query(tmp_path, spec(tmp_path, "bracket", [bracket], DatasetType.BRACKETS))
    assert list(q.read_rules(DatasetType.BRACKETS, T + timedelta(minutes=1), END, cutoff=END)) == []
    ready = registry_readiness(
        HistoricalReconciliation(q), T + timedelta(minutes=1), END, cutoff=END
    )
    assert ready.historical_brackets == "MISSING"


def test_import_conflicting_rows_never_claims_complete_coverage(tmp_path: Path) -> None:
    changed = kline(0)
    changed[4] = "102"
    registry, q = query(tmp_path, spec(tmp_path, "a", [kline(), changed]))
    with connection(registry.path) as db:
        assert db.execute("SELECT coverage,conflicts FROM segments").fetchone() == ("CONFLICTED", 1)
        assert db.execute("SELECT state FROM datasets").fetchone() == ("COMPLETE_IMPORT",)
    assert (
        HistoricalReconciliation(q).evaluate(DatasetType.TRADE, T, END, cutoff=END).coverage_status
        == "CONFLICTED"
    )


def test_nested_bracket_validities_remain_conflicted(tmp_path: Path) -> None:
    middle, last = rule(DatasetType.BRACKETS), rule(DatasetType.BRACKETS)
    middle["valid_from"] = (T + timedelta(minutes=1)).isoformat()
    middle["market_available_at"] = middle["valid_from"]
    middle["valid_until"] = (T + timedelta(seconds=90)).isoformat()
    last["valid_from"] = (T + timedelta(minutes=2)).isoformat()
    last["market_available_at"] = last["valid_from"]
    _, q = query(
        tmp_path,
        spec(tmp_path, "a", [rule(DatasetType.BRACKETS)], DatasetType.BRACKETS),
        spec(tmp_path, "b", [middle], DatasetType.BRACKETS),
        spec(tmp_path, "c", [last], DatasetType.BRACKETS),
    )
    report = HistoricalReconciliation(q).evaluate(DatasetType.BRACKETS, T, END, cutoff=END)
    assert len(report.conflicts) == 2


def test_same_economics_distinct_rule_provenance(tmp_path: Path) -> None:
    alternate = rule(DatasetType.FEES)
    alternate["source"] = "different:SYNTHETIC_FIXTURE"
    alternate["source_version"] = "another-version"
    alternate["authentication_status"] = "SOURCE_DECLARED"
    alternate["maker_rate"] = "0.0001000"
    _, q = query(
        tmp_path,
        spec(tmp_path, "a", [rule(DatasetType.FEES)], DatasetType.FEES),
        spec(tmp_path, "b", [alternate], DatasetType.FEES),
    )
    events = list(q.read_rules(DatasetType.FEES, T, END, cutoff=END))
    assert len(events) == 1
    assert len(events[0].dataset_ids) == 2
    assert events[0].sources == ("local:a", "local:b")
    assert events[0].duplicates == 1
