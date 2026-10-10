"""SYNTHETIC_FIXTURE: garantías parciales declaradas; ningún motor financiero."""

import csv
import json
import socket
import sqlite3
import zipfile
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Inexact, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from trading_engine.replay.historical_validation.contracts import DatasetType
from trading_engine.replay.historical_validation.fast_query import (
    SegmentedHistoricalQuery,
    historical_query,
)
from trading_engine.replay.historical_validation.query import (
    BlockedHistoricalInterval,
    HistoricalQuery,
)
from trading_engine.replay.historical_validation.reconciliation import HistoricalReconciliation
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry, digest
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.segments import Registration, SegmentBounds
from trading_engine.replay.historical_validation.verification import (
    SessionPolicy,
    VerificationMode,
    VerificationState,
)

from .test_historical_registry import END, VALIDATED, T, funding, kline, rule, spec

POLICY = SessionPolicy(3600, 1000, "Local fixture immutable during controlled session")


def session(
    path: Path, ids: tuple[str, ...], name: str = "fixture-session"
) -> SealedSegmentSession:
    return SealedSegmentSession(
        path, ids, session_identity=name, opened_at=VALIDATED, policy=POLICY
    )


def registered(tmp_path: Path, registration: Registration) -> tuple[Path, tuple[str, ...]]:
    path = tmp_path / "registry.sqlite3"
    result = HistoricalDatasetRegistry(path).register(registration)
    return path, (result.dataset_id,)


@pytest.mark.parametrize("kind", list(DatasetType)[:6])
@pytest.mark.parametrize("file_format", ["jsonl", "json"])
def test_all_categories_match_strict(tmp_path: Path, kind: DatasetType, file_format: str) -> None:
    rows: list[object] = (
        [kline(i) for i in range(3)]
        if kind in {DatasetType.TRADE, DatasetType.MARK}
        else [funding(i) for i in range(3)]
        if kind is DatasetType.FUNDING
        else [rule(kind)]
    )
    registration = spec(tmp_path, kind.name, rows, kind, file_format=file_format)
    path, ids = registered(tmp_path, registration)
    strict = HistoricalQuery(path, ids)
    with session(path, ids) as active:
        fast = SegmentedHistoricalQuery(active)
        for cutoff in (T + timedelta(seconds=30), T + timedelta(minutes=2), END):
            assert list(fast.read(kind, T, END, cutoff=cutoff)) == list(
                strict.read(kind, T, END, cutoff=cutoff)
            )
        assert HistoricalReconciliation(fast).evaluate(
            kind, T, END, cutoff=END
        ) == HistoricalReconciliation(strict).evaluate(kind, T, END, cutoff=END)


def test_strict_default_and_opt_in(tmp_path: Path) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    assert type(historical_query(path, ids)) is HistoricalQuery
    with pytest.raises(ValueError, match="sesión explícita"):
        historical_query(path, ids, mode=VerificationMode.SEALED_SEGMENT_SESSION)
    with session(path, ids) as active:
        assert isinstance(
            historical_query(
                path, ids, mode=VerificationMode.SEALED_SEGMENT_SESSION, session=active
            ),
            SegmentedHistoricalQuery,
        )
        with pytest.raises(ValueError):
            historical_query(path, (), mode=VerificationMode.SEALED_SEGMENT_SESSION, session=active)
        with pytest.raises(ValueError):
            historical_query(path, ids, session=active)


@pytest.mark.parametrize("change", ["modify", "truncate", "replace", "absent"])
@pytest.mark.parametrize("mode", ["strict", "session"])
def test_original_changed_before_open(tmp_path: Path, change: str, mode: str) -> None:
    registration = spec(tmp_path, "trade", [kline(i) for i in range(3)])
    path, ids = registered(tmp_path, registration)
    original = Path(registration.original_file)
    if change == "absent":
        original.unlink()
    elif change == "replace":
        content = original.read_bytes()
        original.unlink()
        original.write_bytes(content + b" ")
    elif change == "truncate":
        original.write_bytes(original.read_bytes()[:10])
    else:
        original.write_text(original.read_text().replace('"101"', '"102"'))
    with pytest.raises((ValueError, OSError)):
        if mode == "strict":
            list(HistoricalQuery(path, ids).read_trade(T, END, cutoff=END))
        else:
            session(path, ids)


@pytest.mark.parametrize("field", ["records", "segments", "audit", "parser", "schema"])
def test_registry_tamper_before_open(tmp_path: Path, field: str) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    db = sqlite3.connect(path)
    if field == "records":
        db.execute("UPDATE records SET event_id='forged'")
    elif field == "segments":
        db.execute("UPDATE segments SET digest='recalculated-lie'")
    elif field == "audit":
        db.execute("UPDATE datasets SET audit='{}',audit_id=?", (sha256(b"{}").hexdigest(),))
    else:
        text = db.execute("SELECT spec FROM datasets").fetchone()[0]
        data = json.loads(text)
        data[field + "_version"] = "changed" if field == "parser" else 2
        db.execute("UPDATE datasets SET spec=?", (json.dumps(data),))
    db.commit()
    db.close()
    with pytest.raises(ValueError):
        session(path, ids)


@pytest.mark.parametrize(
    "field", ["event_id", "raw_digest", "offset", "length", "byte_digest", "payload", "links"]
)
def test_segment_cache_tamper_against_memory_root(tmp_path: Path, field: str) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline(i) for i in range(3)]))
    with session(path, ids) as active:
        db = sqlite3.connect(active.cache_path)
        if field == "links":
            db.execute("DELETE FROM links WHERE sequence=2")
        elif field in {"offset", "length"}:
            db.execute(f"UPDATE locators SET {field}=1 WHERE sequence=2")
        else:
            db.execute(f"UPDATE locators SET {field}='forged' WHERE sequence=2")
        db.commit()
        db.close()
        with pytest.raises((ValueError, json.JSONDecodeError)):
            SegmentedHistoricalQuery(active).read(DatasetType.TRADE, T, END, cutoff=END)


def test_recalculated_local_digest_is_not_root(tmp_path: Path) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    with session(path, ids) as active:
        db = sqlite3.connect(active.cache_path)
        payload = json.dumps(kline()).replace('"101"', '"102"')
        db.execute(
            "UPDATE locators SET payload=?,byte_digest=?,raw_digest=?",
            (payload, digest(payload), sha256(payload.encode()).hexdigest()),
        )
        db.commit()
        db.close()
        with pytest.raises(ValueError):
            SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END)


@pytest.mark.parametrize("queried", [True, False])
def test_after_full_validation_detects_observable_source_change(
    tmp_path: Path, queried: bool
) -> None:
    registration = spec(tmp_path, "trade", [kline(i) for i in range(3)])
    registration = replace(
        registration,
        segments=(
            SegmentBounds(T, T + timedelta(minutes=1)),
            SegmentBounds(T + timedelta(minutes=1), END),
        ),
    )
    path, ids = registered(tmp_path, registration)
    with session(path, ids) as active:
        original = Path(registration.original_file)
        rows = original.read_text().splitlines()
        index = 0 if queried else 2
        rows[index] = rows[index].replace('"101"', '"102"')
        original.write_text("\n".join(rows))
        with pytest.raises(ValueError, match="SOURCE_CHANGED"):
            SegmentedHistoricalQuery(active).read_trade(T, T + timedelta(minutes=1), cutoff=END)


@pytest.mark.parametrize("queried", [True, False])
def test_partial_verification_does_not_claim_global_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queried: bool
) -> None:
    import trading_engine.replay.historical_validation.segment_session as module

    registration = spec(tmp_path, "trade", [kline(i) for i in range(3)])
    registration = replace(
        registration,
        segments=(
            SegmentBounds(T, T + timedelta(minutes=1)),
            SegmentBounds(T + timedelta(minutes=1), END),
        ),
    )
    path, ids = registered(tmp_path, registration)
    with session(path, ids) as active:
        saved = active.states[ids[0]]
        # Adversario fuera del modelo normal: los indicadores de invalidación no se observan.
        monkeypatch.setattr(module, "source_state", lambda _: saved)
        original = Path(registration.original_file)
        rows = original.read_text().splitlines()
        rows[0 if queried else 2] = rows[0 if queried else 2].replace('"101"', '"102"')
        original.write_text("\n".join(rows))
        if queried:
            with pytest.raises(ValueError, match="SEGMENT_BYTES_CHANGED"):
                SegmentedHistoricalQuery(active).read_trade(T, T + timedelta(minutes=1), cutoff=END)
        else:
            stream = SegmentedHistoricalQuery(active).read(
                DatasetType.TRADE, T, T + timedelta(minutes=1), cutoff=END
            )
            assert len(list(stream)) == 1
            assert stream.verification.source_state is VerificationState.SEGMENT_VERIFIED
            assert stream.verification.original_state is VerificationState.SOURCE_NOT_REVALIDATED
            with pytest.raises(ValueError):
                list(HistoricalQuery(path, ids).read_trade(T, T + timedelta(minutes=1), cutoff=END))


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_checkpoint_exact_same_session_and_restart(tmp_path: Path, position: int) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline(i) for i in range(3)]))
    with session(path, ids) as active:
        query = SegmentedHistoricalQuery(active)
        complete = query.read(DatasetType.TRADE, T, END, cutoff=END)
        expected = list(complete)
        stream = query.read(DatasetType.TRADE, T, END, cutoff=END)
        prefix = [next(stream) for _ in range(position)]
        cp = stream.checkpoint()
        stream.close()
        recovered = query.resume(cp)
        assert prefix + list(recovered) == expected
        assert recovered.causal_digest == complete.causal_digest
        assert recovered.state == "COMPLETE"
    with session(path, ids, "new-process-root") as fresh:
        query = SegmentedHistoricalQuery(fresh)
        with pytest.raises(ValueError, match="INDEPENDENT_CHECKPOINT_ROOT_REQUIRED"):
            query.resume(cp)
        recovered = query.resume(cp, trusted_checkpoint_digest=cp.checkpoint_digest)
        assert prefix + list(recovered) == expected
        assert recovered.causal_digest == complete.causal_digest


@pytest.mark.parametrize(
    "field",
    [
        "cursor",
        "selection",
        "dataset",
        "parser",
        "schema",
        "segment",
        "source",
        "digest",
        "preflight",
        "cutoff",
        "last",
        "cursors",
    ],
)
def test_checkpoint_recalculated_forgery_rejected(tmp_path: Path, field: str) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline(i) for i in range(3)]))
    with session(path, ids) as active:
        query = SegmentedHistoricalQuery(active)
        stream = query.read(DatasetType.TRADE, T, END, cutoff=END)
        next(stream)
        cp = stream.checkpoint()
        stream.close()
        changes: dict[str, Any] = {
            "cursor": {"cursor": 999},
            "digest": {"causal_digest": "forged"},
            "preflight": {"preflight_digest": "forged"},
            "cutoff": {"cutoff": T},
            "last": {"last_verified_event": "forged"},
            "cursors": {"source_cursors": ((ids[0], 999),)},
        }
        verification_changes: dict[str, dict[str, Any]] = {
            "selection": {"selection_id": "forged"},
            "dataset": {"dataset_ids": ("forged",)},
            "parser": {"parser_version": "forged"},
            "schema": {"schema_version": 2},
            "segment": {"segment_digests": ("forged",)},
            "source": {"source_digests": ("forged",)},
        }
        if field in changes:
            forged = replace(cp, **changes[field], checkpoint_digest="")
        else:
            forged = replace(
                cp,
                verification=replace(cp.verification, **verification_changes[field]),
                checkpoint_digest="",
            )
        forged = replace(forged, checkpoint_digest=digest(forged))
        with pytest.raises(ValueError):
            query.resume(forged)
        with pytest.raises(ValueError):
            query.resume(forged, trusted_checkpoint_digest=cp.checkpoint_digest)
        # Independientemente reconstruir también bloquea contexto falsificado con raíz errónea.
        with pytest.raises(ValueError):
            query.resume(forged, trusted_checkpoint_digest=forged.checkpoint_digest)


def test_provisional_finalization_and_mutation_during_delivery(tmp_path: Path) -> None:
    registration = spec(tmp_path, "trade", [kline(i) for i in range(3)])
    path, ids = registered(tmp_path, registration)
    with session(path, ids) as active:
        query = SegmentedHistoricalQuery(active)
        stream = query.read(DatasetType.TRADE, T, END, cutoff=END)
        assert stream.state == "PROVISIONAL"
        next(stream)
        Path(registration.original_file).write_text("changed")
        with pytest.raises(ValueError, match="SOURCE_CHANGED"):
            next(stream)
        assert stream.state == "FAILED" and stream.reason
        with pytest.raises(ValueError):
            stream.checkpoint()


def test_interruption_cleanup_and_frozen_checkpoint(tmp_path: Path) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    with session(path, ids) as active:
        stream = SegmentedHistoricalQuery(active).read(DatasetType.TRADE, T, END, cutoff=END)
        cp = stream.checkpoint()
        with pytest.raises(FrozenInstanceError):
            cp.cursor = 99  # type: ignore[misc]
        spool = stream.path
        stream.close()
        assert stream.state == "INTERRUPTED" and not spool.exists()
    assert not active.cache_path.exists()


@pytest.mark.parametrize("rule_overlap", [False, True])
def test_conflicts_and_prior_prefix_match(tmp_path: Path, rule_overlap: bool) -> None:
    kind = DatasetType.BRACKETS if rule_overlap else DatasetType.TRADE
    rows: list[object]
    if rule_overlap:
        a, b = rule(kind), rule(kind)
        b["valid_from"] = (T + timedelta(minutes=2)).isoformat()
        b["market_available_at"] = b["valid_from"]
        rows = [a, b]
    else:
        bad = kline(2)
        bad[4] = "102"
        rows = [kline(0), kline(1), kline(2), bad]
    path, ids = registered(tmp_path, spec(tmp_path, "conflict", rows, kind))
    strict = HistoricalQuery(path, ids)
    with session(path, ids) as active:
        fast = SegmentedHistoricalQuery(active)
        assert list(fast.groups(kind, T, END, cutoff=END)) == list(
            strict.groups(kind, T, END, cutoff=END)
        )
        with pytest.raises(BlockedHistoricalInterval):
            fast.read(kind, T, END, cutoff=END)
        assert list(fast.read(kind, T, END, cutoff=T + timedelta(minutes=1))) == list(
            strict.read(kind, T, END, cutoff=T + timedelta(minutes=1))
        )


@pytest.mark.parametrize("file_format", ["csv", "jsonl", "zip"])
def test_exact_physical_boundaries_or_derived_zip(tmp_path: Path, file_format: str) -> None:
    registration = spec(
        tmp_path,
        "trade",
        [kline(i) for i in range(3)],
        file_format="csv" if file_format == "csv" else "jsonl",
    )
    if file_format == "zip":
        archive = tmp_path / "trade.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.write(registration.original_file, "member.jsonl")
        registration = replace(registration, original_file=str(archive), member="member.jsonl")
    elif file_format == "jsonl":
        original = Path(registration.original_file)
        original.write_bytes(b"\xef\xbb\xbf\n\n" + original.read_bytes() + b"\n")
    else:
        original = Path(registration.original_file)
        rows = [kline(i) for i in range(3)]
        # CSV quoted numeric field physically spans lines? Invalid numeric must not be invented.
        # CRLF, quoted values and a header demonstrate row number != offset.
        with original.open("w", newline="") as out:
            writer = csv.writer(out, quoting=csv.QUOTE_ALL)
            from trading_engine.binance.klines import CSV_HEADER

            writer.writerow(CSV_HEADER)
            writer.writerows(rows)
    path, ids = registered(tmp_path, registration)
    with session(path, ids) as active:
        fast = SegmentedHistoricalQuery(active)
        assert list(fast.read_trade(T + timedelta(minutes=2), END, cutoff=END)) == list(
            HistoricalQuery(path, ids).read_trade(T + timedelta(minutes=2), END, cutoff=END)
        )
        db = sqlite3.connect(active.cache_path)
        offsets = db.execute(
            "SELECT offset,length,payload FROM locators ORDER BY sequence"
        ).fetchall()
        db.close()
        assert (
            all(o[0] == -1 and o[2] is not None for o in offsets)
            if file_format == "zip"
            else all(o[0] >= 0 and o[1] > 0 and o[2] is None for o in offsets)
        )


def test_zip_corrupt_before_session(tmp_path: Path) -> None:
    registration = spec(tmp_path, "trade", [kline()])
    archive = tmp_path / "trade.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(registration.original_file, "member.jsonl")
    registration = replace(registration, original_file=str(archive), member="member.jsonl")
    path, ids = registered(tmp_path, registration)
    archive.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        session(path, ids)


def test_stable_registry_snapshot_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket, "socket", deny)
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline(i) for i in range(3)]))
    with session(path, ids) as active:
        baseline = list(HistoricalQuery(path, ids).read_trade(T, END, cutoff=END))
        db = sqlite3.connect(path)
        db.execute("UPDATE records SET event_id='forged'")
        db.commit()
        db.close()
        assert list(SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END)) == baseline
        with pytest.raises(ValueError):
            session(path, ids)


def test_hostile_decimal_and_session_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trading_engine.replay.historical_validation.segment_session as module

    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    with session(path, ids) as active:
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            assert len(list(SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END))) == 1
        monkeypatch.setattr(module, "monotonic", lambda: active._started + POLICY.max_seconds + 1)
        with pytest.raises(ValueError, match="SESSION_EXPIRED"):
            SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END)


@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_policy_requires_real_positive_bounds(value: Any) -> None:
    with pytest.raises(ValueError):
        SessionPolicy(value, 1, "explicit")


def test_checkpoint_json_roundtrip_and_invalid_json(tmp_path: Path) -> None:
    from trading_engine.replay.encoding import encoded
    from trading_engine.replay.historical_validation.verification import checkpoint_from_json

    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    with session(path, ids) as active:
        stream = SegmentedHistoricalQuery(active).read(DatasetType.TRADE, T, END, cutoff=END)
        cp = stream.checkpoint()
        stream.close()
        assert checkpoint_from_json(encoded(cp).decode()) == cp
        for text in ("[]", '{"cursor":1,"cursor":2}', '{"cursor":NaN}', "x" * 1048577):
            with pytest.raises(ValueError):
                checkpoint_from_json(text)


@pytest.mark.parametrize("kind", [DatasetType.BRACKETS, DatasetType.FEES, DatasetType.SYMBOL_RULES])
def test_unknown_rule_quality_blocks_both_modes(tmp_path: Path, kind: DatasetType) -> None:
    raw = rule(kind)
    raw["quality"] = "CURRENT_ONLY"
    path, ids = registered(tmp_path, spec(tmp_path, "rule", [raw], kind))
    with session(path, ids) as active:
        for query in (HistoricalQuery(path, ids), SegmentedHistoricalQuery(active)):
            with pytest.raises(ValueError, match="NOT_CAUSALLY_USABLE"):
                list(query.read_rules(kind, T, END, cutoff=END))


@pytest.mark.parametrize("conflict", [False, True])
def test_monthly_daily_gap_duplicates_and_corrections(tmp_path: Path, conflict: bool) -> None:
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    month = registry.register(spec(tmp_path, "month", [kline(0), kline(2)]))
    daily = registry.register(spec(tmp_path, "daily", [kline(1)]))
    corrected = kline(2)
    if conflict:
        corrected[4] = "102"
    duplicate = registry.register(spec(tmp_path, "duplicate", [corrected]))
    ids = (month.dataset_id, daily.dataset_id, duplicate.dataset_id)
    with session(registry.path, ids) as active:
        strict, fast = HistoricalQuery(registry.path, ids), SegmentedHistoricalQuery(active)
        a = HistoricalReconciliation(strict).evaluate(DatasetType.TRADE, T, END, cutoff=END)
        b = HistoricalReconciliation(fast).evaluate(DatasetType.TRADE, T, END, cutoff=END)
        assert a == b
        assert b.coverage_status == ("CONFLICTED" if conflict else "AVAILABLE_FOR_REVIEW")
        assert b.duplicates == (0 if conflict else 1)


def test_nested_rule_overlaps_and_gaps(tmp_path: Path) -> None:
    a = rule(DatasetType.BRACKETS)
    b = dict(a)
    b["valid_from"] = (T + timedelta(minutes=1)).isoformat()
    b["valid_until"] = (T + timedelta(minutes=2)).isoformat()
    b["market_available_at"] = b["valid_from"]
    c = dict(a)
    c["valid_from"] = (T + timedelta(minutes=2)).isoformat()
    c["market_available_at"] = c["valid_from"]
    path, ids = registered(tmp_path, spec(tmp_path, "nested", [a, b, c], DatasetType.BRACKETS))
    with session(path, ids) as active:
        a_report = HistoricalReconciliation(HistoricalQuery(path, ids)).evaluate(
            DatasetType.BRACKETS, T, END, cutoff=END
        )
        b_report = HistoricalReconciliation(SegmentedHistoricalQuery(active)).evaluate(
            DatasetType.BRACKETS, T, END, cutoff=END
        )
        assert a_report == b_report and len(b_report.conflicts) == 2


def test_session_query_limit_and_closed_session(tmp_path: Path) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    active = SealedSegmentSession(
        path,
        ids,
        session_identity="limited",
        opened_at=VALIDATED,
        policy=SessionPolicy(60, 1, "fixture"),
    )
    assert len(list(SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END))) == 1
    with pytest.raises(ValueError, match="SESSION_QUERY_LIMIT"):
        SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END)
    active.close()
    with pytest.raises(ValueError, match="VERIFICATION_NOT_AVAILABLE"):
        SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END)


def test_failure_during_index_is_atomic_and_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trading_engine.replay.historical_validation.segment_session as module

    registration = spec(tmp_path, "trade", [kline()])
    path, ids = registered(tmp_path, registration)
    expected = Path(registration.original_file).read_bytes()
    expected_db = path.read_bytes()

    def fail(_: object) -> Any:
        raise OSError("fixture preparation failure")

    monkeypatch.setattr(module, "physical_rows", fail)
    with pytest.raises(OSError):
        session(path, ids)
    assert (
        Path(registration.original_file).read_bytes() == expected
        and path.read_bytes() == expected_db
    )


def test_spool_tamper_cannot_finalize_success(tmp_path: Path) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    with session(path, ids) as active:
        stream = SegmentedHistoricalQuery(active).read(DatasetType.TRADE, T, END, cutoff=END)
        # La conexión snapshot protege cambios posteriores en disco; una corrupción del
        # snapshot visible se prueba con la propia conexión, sin afirmar autenticidad.
        stream.db.execute("ROLLBACK")
        stream.db.execute("PRAGMA query_only=OFF")
        stream.db.execute("DELETE FROM results")
        stream.db.commit()
        stream._rows = iter(
            stream.db.execute("SELECT sequence,payload FROM results ORDER BY sequence")
        )
        with pytest.raises(ValueError, match="QUERY_SNAPSHOT_CHANGED"):
            next(stream)
        assert stream.state == "FAILED"


@pytest.mark.parametrize("change", ["hide", "digest", "bounds", "extra"])
def test_temporal_segment_index_cannot_hide_tampering(tmp_path: Path, change: str) -> None:
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline()]))
    with session(path, ids) as active:
        db = sqlite3.connect(active.cache_path)
        if change == "hide":
            db.execute("DELETE FROM segments")
        elif change == "digest":
            db.execute("UPDATE segments SET digest='forged'")
        elif change == "bounds":
            db.execute("UPDATE segments SET start=end")
        else:
            db.execute(
                "INSERT INTO segments SELECT 'extra',dataset,start,end,row_count,digest "
                "FROM segments"
            )
        db.commit()
        db.close()
        with pytest.raises(ValueError, match="SEGMENT_SELECTION_INDEX_CHANGED"):
            SegmentedHistoricalQuery(active).read_trade(T, END, cutoff=END)


def test_physical_record_limit_before_materialization(tmp_path: Path) -> None:
    from trading_engine.replay.historical_validation.contracts import Limits
    from trading_engine.replay.historical_validation.segment_session import physical_rows

    registration = spec(tmp_path, "trade", [kline()])
    registration = replace(registration, limits=Limits(max_line_bytes=256))
    original = Path(registration.original_file)
    original.write_text("\n" * 3073 + original.read_text())
    with pytest.raises(ValueError, match="PHYSICAL_RECORD_LIMIT"):
        next(physical_rows(registration))


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_conflicted_audit_stream_recovery(tmp_path: Path, position: int) -> None:
    bad = kline(2)
    bad[4] = "102"
    path, ids = registered(tmp_path, spec(tmp_path, "trade", [kline(0), kline(1), kline(2), bad]))
    with session(path, ids) as active:
        query = SegmentedHistoricalQuery(active)
        complete = query.groups(DatasetType.TRADE, T, END, cutoff=END)
        expected = list(complete)
        interrupted = query.groups(DatasetType.TRADE, T, END, cutoff=END)
        prefix = [next(interrupted) for _ in range(position)]
        cp = interrupted.checkpoint()
        interrupted.close()
        resumed = query.resume_groups(cp)
        assert prefix + list(resumed) == expected
        assert resumed.causal_digest == complete.causal_digest and resumed.state == "COMPLETE"
        with pytest.raises(BlockedHistoricalInterval):
            query.resume(cp)
