"""Fixtures exclusivamente sintéticos: contratos, seguridad, cobertura y causalidad."""

import ast
import csv
import io
import json
import socket
import zipfile
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from hashlib import sha256
from pathlib import Path

import pytest

from trading_engine.binance.klines import CSV_HEADER, DEFAULT_SYMBOL, LAUNCH, MARKET, to_ms
from trading_engine.models.market import Candle
from trading_engine.models.values import Symbol
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_performance.evidence import Origin, Reference
from trading_engine.replay.historical_validation.audit import (
    FundingInventory,
    audit_file,
    audit_rows,
    coverage_calendar,
)
from trading_engine.replay.historical_validation.catalog import CATALOG
from trading_engine.replay.historical_validation.contracts import (
    Authentication,
    Contract,
    DatasetDeclaration,
    DatasetType,
    Limits,
    Readiness,
    units,
)
from trading_engine.replay.historical_validation.local_files import local_rows
from trading_engine.replay.historical_validation.parsers import FundingRate, MarkKline, parse_record
from trading_engine.replay.historical_validation.report import DatasetAudit, readiness_report
from trading_engine.replay.historical_validation.rules import HistoricalBrackets, parse_rule
from trading_engine.replay.historical_validation.sqlite_audit import audit_sqlite

T = LAUNCH
END = T + timedelta(minutes=3)


def declaration(kind: DatasetType = DatasetType.TRADE, end: datetime = END) -> DatasetDeclaration:
    return DatasetDeclaration(
        "synthetic-" + kind.value,
        kind,
        Contract(),
        "1m" if kind in {DatasetType.TRADE, DatasetType.MARK} else "EVENTS",
        T,
        end,
        "https://example.invalid/declared-only",
        "SYNTHETIC_FIXTURE_NOT_OFFICIAL",
        end + timedelta(days=1),
        "historical-local-v1",
        units(kind),
    )


def kline(index: int = 0) -> list[object]:
    opened = to_ms(T + timedelta(minutes=index))
    return [opened, "100", "103", "99", "101", "10", opened + 59999, "1000", 2, "5", "500", "0"]


def funding(index: int = 0) -> dict[str, object]:
    at = T + timedelta(minutes=index)
    return {
        "symbol": DEFAULT_SYMBOL.value,
        "fundingTime": to_ms(at),
        "fundingRate": "0.0001",
        "markPrice": "100",
        "rateType": "HISTORICAL",
        "market_available_at": at.isoformat(),
    }


def rule(kind: DatasetType = DatasetType.BRACKETS) -> dict[str, object]:
    common: dict[str, object] = {
        "symbol": DEFAULT_SYMBOL.value,
        "margin_asset": "USDC",
        "valid_from": T.isoformat(),
        "valid_until": END.isoformat(),
        "market_available_at": T.isoformat(),
        "source": "SYNTHETIC_FIXTURE",
        "source_version": "v1",
        "authentication_status": "NOT_AUTHENTICATED",
        "quality": "VERIFIED_FOR_PERIOD",
    }
    if kind is DatasetType.BRACKETS:
        common["brackets"] = [
            {
                "notional_floor": "0",
                "notional_cap": "10000",
                "maintenance_rate": "0.01",
                "maintenance_amount": "0",
                "max_leverage": "4",
            }
        ]
    elif kind is DatasetType.SYMBOL_RULES:
        common.update(
            tick_size="0.000001",
            step_size="1",
            min_quantity="1",
            max_quantity="100000",
            min_notional="5",
            min_price="0.000001",
            max_price="100000",
            max_leverage="4",
            contract_status="TRADING",
        )
    else:
        common.update(
            maker_rate="0.0001",
            taker_rate="0.0004",
            user_tier="FIXTURE",
            promotion="NONE_DECLARED",
            discount="0",
            fee_currency="USDC",
            effective_role_evidence="UNKNOWN_ACTUAL_FILL_ROLE",
        )
    return common


def write_csv(path: Path, rows: list[list[object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerows(rows)
    return path


def file_report(path: Path, kind: DatasetType = DatasetType.TRADE) -> DatasetAudit:
    return audit_file(
        path,
        declaration(kind),
        validation_time=END + timedelta(days=2),
        as_of=END,
        file_format="csv",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": Symbol("SHIBUSDC")},
        {"symbol": Symbol("1000SHIBUSDT")},
        {"market": "SPOT"},
        {"exchange": "OTHER"},
        {"settlement_asset": "USDT"},
        {"contract_type": "DELIVERY"},
    ],
)
def test_exact_contract_rejects_substitutions(changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        Contract(**changes)  # type: ignore[arg-type]


def test_catalog_all_categories_signed_endpoint_and_immutable() -> None:
    assert {entry.category for entry in CATALOG} == set(DatasetType)
    assert "USER_DATA" in next(e.restriction for e in CATALOG if e.category is DatasetType.BRACKETS)
    assert Contract().market == MARKET
    with pytest.raises(FrozenInstanceError):
        Contract().exchange = "OTHER"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"period_start": T - timedelta(minutes=1)},
        {"interval": "15m"},
        {"unit_contract": "price=SHIBUSDC"},
        {"schema_version": "v0"},
        {"period_start": T.replace(tzinfo=None)},
    ],
)
def test_declaration_rejects_invalid_contract(changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(declaration(), **changes)  # type: ignore[arg-type]


def test_trade_valid_reuses_normalizer_and_59999_close() -> None:
    record = parse_record(kline(), DatasetType.TRADE, as_of=END)
    assert isinstance(record, Candle)
    assert record.open_time == T and record.close_time == T + timedelta(minutes=1)
    assert record.taker_buy_base_volume == Decimal("5")
    report, months = audit_rows(
        [list(CSV_HEADER), kline(), kline(1), kline(2)], declaration(), as_of=END
    )
    assert report.coverage_complete and report.rows == 3 and months[0].coverage_percent == 100
    assert report.first_timestamp == T and report.last_timestamp == END - timedelta(minutes=1)


@pytest.mark.parametrize(
    "field,value",
    [
        (0, to_ms(T) + 1),
        (0, to_ms(T) * 1000),
        (0, True),
        (6, to_ms(T) + 60000),
        (1, 100.0),
        (1, "NaN"),
        (1, "Infinity"),
        (2, "90"),
        (3, "120"),
        (5, "-1"),
        (9, "11"),
        (9, None),
        (10, "1001"),
    ],
)
def test_trade_invalid_fields(field: int, value: object) -> None:
    row = kline()
    row[field] = value
    report, _ = audit_rows([row], declaration(), as_of=END)
    assert report.errors and not report.coverage_complete


def test_prelaunch_and_open_candle_rejected_without_shift() -> None:
    with pytest.raises(ValueError):
        parse_record(kline(-1), DatasetType.TRADE, as_of=END)
    with pytest.raises(ValueError):
        parse_record(kline(), DatasetType.TRADE, as_of=T + timedelta(seconds=59))
    report, _ = audit_rows([kline(-1), kline()], declaration(), as_of=END)
    assert report.errors and not report.causally_usable


def test_gap_duplicates_conflicts_and_unordered_distinct() -> None:
    gap, _ = audit_rows([kline(), kline(2)], declaration(), as_of=END)
    assert gap.gaps[0].missing_minutes == 1
    exact, _ = audit_rows([kline(), kline(), kline(1), kline(2)], declaration(), as_of=END)
    assert exact.duplicates == 1 and exact.rows == 3 and exact.coverage_complete
    equivalent = kline()
    equivalent[1] = "100.000"
    numeric, _ = audit_rows([kline(), equivalent, kline(1), kline(2)], declaration(), as_of=END)
    assert numeric.duplicates == 1 and numeric.conflicts == 0
    auxiliary = kline()
    auxiliary[7] = "999"
    source_conflict, _ = audit_rows([kline(), auxiliary], declaration(), as_of=END)
    assert source_conflict.conflicts == 1
    changed = kline()
    changed[4] = "102"
    conflict, _ = audit_rows([kline(), kline(1), changed], declaration(), as_of=END)
    assert conflict.conflicts == 1 and not conflict.coverage_complete
    unordered, _ = audit_rows([kline(2), kline(), kline(1)], declaration(), as_of=END)
    assert any("orden temporal" in e for e in unordered.errors)


def test_checksum_matches_but_does_not_authenticate(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "fixture.csv", [kline(), kline(1), kline(2)])
    checksum = tmp_path / "checksum.txt"
    checksum.write_text(sha256(path.read_bytes()).hexdigest() + "  fixture.csv\n")
    report = audit_file(
        path,
        declaration(),
        as_of=END,
        validation_time=END + timedelta(days=2),
        file_format="csv",
        checksum_file=checksum,
    )
    assert report.manifest.integrity_status == "INTEGRITY_VALIDATED"
    assert report.manifest.provider_authenticity is Authentication.DECLARED
    readiness = readiness_report((report,), T, END)
    assert readiness.metrics[0].state is Readiness.UNAUTHENTICATED
    assert readiness.runtime == "STOPPED" and readiness.survival == "NOT_CERTIFIED"
    checksum.write_text("0" * 64 + "  fixture.csv\n")
    with pytest.raises(ValueError, match="Checksum contradictorio"):
        audit_file(
            path,
            declaration(),
            as_of=END,
            validation_time=END + timedelta(days=2),
            file_format="csv",
            checksum_file=checksum,
        )


def test_missing_checksum_and_bad_checksum_name(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "fixture.csv", [kline()])
    report = file_report(path)
    assert report.manifest.checksum_verification == "NOT_PROVIDED"
    assert report.manifest.integrity_status == "IDENTITY_CALCULATED_ONLY"
    checksum = tmp_path / "checksum.txt"
    checksum.write_text("0" * 64 + "  other.csv\n")
    with pytest.raises(ValueError, match="otro archivo"):
        audit_file(
            path,
            declaration(),
            as_of=END,
            validation_time=END + timedelta(days=2),
            file_format="csv",
            checksum_file=checksum,
        )


def test_zip_valid_streaming_crc_and_corrupt(tmp_path: Path) -> None:
    payload = io.StringIO()
    csv.writer(payload).writerows([kline(), kline(1), kline(2)])
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fixture.csv", payload.getvalue())
    report = audit_file(
        path,
        declaration(),
        as_of=END,
        validation_time=END + timedelta(days=2),
        file_format="csv",
        member="fixture.csv",
    )
    assert report.semantic.coverage_complete and report.manifest.archive_name == "fixture.zip"
    path.write_bytes(b"invalid zip")
    with pytest.raises(zipfile.BadZipFile):
        audit_file(
            path,
            declaration(),
            as_of=END,
            validation_time=END + timedelta(days=2),
            file_format="csv",
            member="fixture.csv",
        )


@pytest.mark.parametrize("member", ["../fixture.csv", "/fixture.csv", "a/fixture.csv", "a\\f.csv"])
def test_zip_path_traversal_rejected(tmp_path: Path, member: str) -> None:
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, "x")
    with pytest.raises(ValueError):
        list(local_rows(path, Limits(), file_format="csv", member=member))


def test_zip_bomb_symlink_and_duplicate_members_rejected(tmp_path: Path) -> None:
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fixture.csv", "0" * 20000)
    with pytest.raises(ValueError, match="límites"):
        list(
            local_rows(
                path, Limits(max_compression_ratio=2), file_format="csv", member="fixture.csv"
            )
        )
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("fixture.csv")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "target")
    with pytest.raises(ValueError, match="symlink"):
        list(local_rows(path, Limits(), file_format="csv", member="fixture.csv"))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fixture.csv", "x")
        archive.writestr("other.csv", "x")
    with pytest.raises(ValueError, match="exactamente un miembro"):
        list(local_rows(path, Limits(), file_format="csv", member="fixture.csv"))


@pytest.mark.parametrize("kind", [DatasetType.TRADE, DatasetType.MARK])
def test_future_payload_does_not_change_prefix(kind: DatasetType) -> None:
    until = T + timedelta(minutes=1)
    first, _ = audit_rows([kline()], declaration(kind), as_of=until)
    future = kline(1)
    future[4] = "malicious future economic value"
    second, _ = audit_rows([kline(), future, kline(2)], declaration(kind), as_of=until)
    assert first == second


def test_mark_closed_ohlc_not_trade_tick_or_financial_evidence() -> None:
    mark = parse_record(kline(), DatasetType.MARK, as_of=END)
    assert isinstance(mark, MarkKline) and not isinstance(mark, Candle)
    assert mark.market_available_at == T + timedelta(minutes=1)
    assert mark.intraminute_path == "UNKNOWN_INTRAMINUTE_PATH"
    with pytest.raises(ValueError, match="todavía no disponible"):
        mark.close_sample(T + timedelta(seconds=59))
    assert mark.close_sample(T + timedelta(minutes=1)) == Decimal("101")


def test_funding_requires_inventory_and_does_not_infer_eight_hours() -> None:
    rows = [funding(), funding(1), funding(2)]
    missing, _ = audit_rows(rows, declaration(DatasetType.FUNDING), as_of=END)
    assert (
        not missing.coverage_complete
        and missing.funding_inventory_status == "FUNDING_HISTORY_INCOMPLETE"
    )
    inventory = FundingInventory(
        T,
        END,
        (T, T + timedelta(minutes=1), T + timedelta(minutes=2)),
        "EXPLICIT_SYNTHETIC_CALENDAR_CHANGE",
        END,
    )
    complete, _ = audit_rows(
        rows, declaration(DatasetType.FUNDING), as_of=END, funding_inventory=inventory
    )
    assert complete.coverage_complete
    duplicate, _ = audit_rows(
        rows + [funding(2)],
        declaration(DatasetType.FUNDING),
        as_of=END,
        funding_inventory=inventory,
    )
    assert duplicate.duplicates == 1 and not duplicate.coverage_complete
    assert duplicate.funding_inventory_status == "FUNDING_HISTORY_INCOMPLETE"
    omitted, _ = audit_rows(
        rows[:2], declaration(DatasetType.FUNDING), as_of=END, funding_inventory=inventory
    )
    assert not omitted.coverage_complete
    empty, _ = audit_rows(
        [], declaration(DatasetType.FUNDING), as_of=END, funding_inventory=inventory
    )
    assert empty.rows == 0 and not empty.coverage_complete


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "1000SHIBUSDT"},
        {"fundingTime": to_ms(T - timedelta(minutes=1))},
        {"fundingRate": 0.0001},
        {"fundingRate": "NaN"},
        {"markPrice": None},
        {"market_available_at": (T - timedelta(seconds=1)).isoformat()},
    ],
)
def test_funding_invalid_contract_and_reference(changes: dict[str, object]) -> None:
    row = funding()
    row.update(changes)
    result, _ = audit_rows([row], declaration(DatasetType.FUNDING), as_of=END)
    assert result.errors and not result.coverage_complete


def test_funding_not_payment_and_download_not_effective_time() -> None:
    result = parse_record(funding(), DatasetType.FUNDING, as_of=END)
    assert isinstance(result, FundingRate) and result.rate == Decimal("0.0001")
    assert result.market_effective_at == T
    assert result.economic_use == "HISTORICAL_RATE_NOT_USER_PAYMENT"
    assert declaration().acquisition_time != result.market_effective_at


@pytest.mark.parametrize("kind", [DatasetType.SYMBOL_RULES, DatasetType.BRACKETS, DatasetType.FEES])
def test_temporal_rules_valid_and_current_not_retrospective(kind: DatasetType) -> None:
    complete, _ = audit_rows([rule(kind)], declaration(kind), as_of=END)
    assert complete.coverage_complete and complete.causally_usable
    current = rule(kind)
    current["quality"] = "CURRENT_ONLY"
    current["market_available_at"] = (T + timedelta(minutes=1)).isoformat()
    noncausal, _ = audit_rows([current], declaration(kind), as_of=END)
    assert not noncausal.causally_usable
    launch_only = rule(kind)
    launch_only["quality"] = "DOCUMENTED_AT_LAUNCH"
    historical, _ = audit_rows([launch_only], declaration(kind), as_of=END)
    assert not historical.causally_usable
    expired = rule(kind)
    expired["valid_until"] = (T + timedelta(minutes=1)).isoformat()
    gap, _ = audit_rows([expired], declaration(kind), as_of=END)
    assert gap.gaps and not gap.coverage_complete


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "1000SHIBUSDT"},
        {"margin_asset": "USDT"},
        {"brackets": []},
        {"authentication_status": "SOURCE_AUTHENTICATED"},
        {"source": None},
    ],
)
def test_brackets_wrong_contract_or_authenticity(changes: dict[str, object]) -> None:
    row = rule()
    row.update(changes)
    result, _ = audit_rows([row], declaration(DatasetType.BRACKETS), as_of=END)
    assert result.errors


def test_missing_mmr_user_adjustment_and_fee_rules() -> None:
    row = rule()
    row["brackets"] = [
        {
            "notional_floor": "0",
            "notional_cap": "10000",
            "maintenance_amount": "0",
            "max_leverage": "4",
        }
    ]
    with pytest.raises(KeyError):
        parse_rule(row, DatasetType.BRACKETS)
    valid = rule()
    valid.update(user_adjustment_multiplier="1.1", user_scope="DECLARED_NOT_ACCOUNT_QUERY")
    parsed = parse_rule(valid, DatasetType.BRACKETS)
    assert isinstance(parsed, HistoricalBrackets) and parsed.user_adjustment_multiplier == Decimal(
        "1.1"
    )
    fees = rule(DatasetType.FEES)
    del fees["taker_rate"]
    incomplete, _ = audit_rows([fees], declaration(DatasetType.FEES), as_of=END)
    assert incomplete.errors and not incomplete.coverage_complete


def test_months_launch_pre_contract_and_missing(tmp_path: Path) -> None:
    report = file_report(write_csv(tmp_path / "fixture.csv", [kline(), kline(1), kline(2)]))
    matrix = coverage_calendar(report, datetime(2024, 2, 1, tzinfo=UTC), END)
    assert matrix[0].utilization == "PRE_CONTRACT"
    assert matrix[1].utilization == "PRE_CONTRACT" and matrix[1].end == LAUNCH
    assert matrix[2].utilization == "AVAILABLE" and matrix[2].coverage_percent == 100
    readiness = readiness_report((), T, END)
    assert len(readiness.metrics) == 8
    assert all(m.state is Readiness.MISSING for m in readiness.metrics[:7])
    assert all(m.reasons == ("DATASET_NOT_PROVIDED",) for m in readiness.metrics[:7])


def test_50_precision_and_hostile_decimal_context() -> None:
    high_precision = "100.1234567890123456789012345678901234567890123456789"
    rows = [kline(), kline(1), kline(2)]
    rows[0][1] = high_precision
    reference, months = audit_rows(rows, declaration(), as_of=END)
    with localcontext() as hostile:
        hostile.prec = 2
        hostile.traps[Inexact] = True
        actual, actual_months = audit_rows(rows, declaration(), as_of=END)
        fee = rule(DatasetType.FEES)
        fee["maker_rate"] = "0.99999999999999999999999999999"
        parse_rule(fee, DatasetType.FEES)
    assert reference == actual and months == actual_months
    record = parse_record(rows[0], DatasetType.TRADE, as_of=END)
    assert isinstance(record, Candle) and record.open.value == Decimal(high_precision)


@pytest.mark.parametrize(
    "payload",
    [
        '{"symbol":"A","symbol":"B"}\n',
        '{"rate":NaN}\n',
        '{"rate":Infinity}\n',
    ],
)
def test_malicious_json_rejected(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "fixture.jsonl"
    path.write_text(payload)
    with pytest.raises(ValueError):
        list(local_rows(path, Limits(), file_format="jsonl"))


def test_json_and_jsonl_bounded_no_implicit_floats(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps([kline(), kline(1), kline(2)]))
    result = audit_file(
        path, declaration(), validation_time=END + timedelta(days=2), as_of=END, file_format="json"
    )
    assert result.semantic.rows == 3
    with pytest.raises(ValueError, match="max_json_bytes"):
        list(local_rows(path, Limits(max_json_bytes=10), file_format="json"))
    path.write_text("x" * 100)
    with pytest.raises(ValueError, match="línea"):
        list(local_rows(path, Limits(max_line_bytes=10), file_format="jsonl"))


def test_streaming_row_file_and_findings_limits(tmp_path: Path) -> None:
    consumed = 0

    def rows() -> Iterator[list[object]]:
        nonlocal consumed
        for i in range(1000):
            consumed += 1
            yield kline(i)

    with pytest.raises(ValueError, match="max_rows"):
        audit_rows(
            rows(),
            declaration(end=T + timedelta(days=1)),
            as_of=T + timedelta(days=1),
            limits=Limits(max_rows=3),
        )
    assert consumed == 4
    with pytest.raises(ValueError, match="max_findings"):
        audit_rows([[], [], []], declaration(), as_of=END, limits=Limits(max_findings=1))
    path = write_csv(tmp_path / "fixture.csv", [kline()])
    with pytest.raises(ValueError, match="max_file_bytes"):
        audit_file(
            path,
            declaration(),
            validation_time=END + timedelta(days=2),
            as_of=END,
            file_format="csv",
            limits=Limits(max_file_bytes=1),
        )


def test_sqlite_existing_pipeline_readonly_integrity_manifests(tmp_path: Path) -> None:
    path = tmp_path / "fixture.sqlite3"
    candles: list[Candle] = []
    for i in range(3):
        candle = parse_record(kline(i), DatasetType.TRADE, as_of=END)
        assert isinstance(candle, Candle)
        candles.append(candle)
    with FuturesSQLite(path) as store:
        store.import_candles(
            candles,
            symbol=DEFAULT_SYMBOL,
            source=Source.REST,
            file="synthetic",
            period="fixture",
            as_of=END,
        )
        store.note("failed-fixture", "fixture", Source.REST, "failed", END, error="fixture")
    before = sha256(path.read_bytes()).hexdigest()
    result = audit_sqlite(path, declaration(), as_of=END)
    assert result.coverage.complete and result.coverage.rows == 3
    assert result.manifests.complete == 1 and result.manifests.failed == 1
    assert result.manifests.without_checksum == 2 and result.outside_contract_rows == 0
    assert result.causal_status.startswith("BLOCKED")
    assert sha256(path.read_bytes()).hexdigest() == before
    with FuturesSQLite(path, read_only=True) as store:
        with pytest.raises(Exception, match="readonly"):
            store.connection.execute("DELETE FROM futures_klines_1m")
    with pytest.raises(ValueError, match="únicamente Trade"):
        audit_sqlite(path, declaration(DatasetType.MARK), as_of=END)


def test_no_network_or_trading_and_observed_still_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    report = file_report(write_csv(tmp_path / "fixture.csv", [kline(), kline(1), kline(2)]))
    assert report.semantic.coverage_complete
    package = Path(__file__).parents[3] / "src/trading_engine/replay/historical_validation"
    forbidden_imports = {"socket", "requests", "urllib", "http", "subprocess"}
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(n.name.split(".")[0] not in forbidden_imports for n in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_imports
                assert not node.module.endswith("public_data")
    with pytest.raises(ValueError, match="OBSERVED"):
        Reference("fixture", "fixture", Origin.OBSERVED, "Not authorized")
    assert b"NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES" in encoded(report)


@pytest.mark.parametrize(
    "changes",
    [
        {"market": "SPOT"},
        {"exchange": "OTHER"},
        {"settlement_asset": "USDT"},
        {"dataset_type": "TRADE_KLINES_1M"},
    ],
)
def test_funding_contradictory_metadata_blocked(changes: dict[str, object]) -> None:
    row = funding()
    row.update(changes)
    result, _ = audit_rows([row], declaration(DatasetType.FUNDING), as_of=END)
    assert result.errors and not result.causally_usable


@pytest.mark.parametrize("kind", [DatasetType.FUNDING, DatasetType.BRACKETS, DatasetType.FEES])
def test_future_event_does_not_change_prefix(kind: DatasetType) -> None:
    until = T + timedelta(minutes=1)
    first = funding() if kind is DatasetType.FUNDING else rule(kind)
    future = funding(2) if kind is DatasetType.FUNDING else rule(kind)
    if kind is not DatasetType.FUNDING:
        future["valid_from"] = (T + timedelta(minutes=2)).isoformat()
        future["market_available_at"] = (T + timedelta(minutes=2)).isoformat()
    future["symbol"] = "INVALID_FUTURE_CONTRACT"
    before, _ = audit_rows([first], declaration(kind), as_of=until)
    after, _ = audit_rows([first, future], declaration(kind), as_of=until)
    assert before == after


def test_temporal_rule_covering_query_from_earlier_valid_from() -> None:
    start = T + timedelta(minutes=1)
    query = replace(declaration(DatasetType.BRACKETS), period_start=start)
    result, months = audit_rows([rule()], query, as_of=END)
    assert result.coverage_complete and result.first_timestamp == T
    assert months[0].utilization == "AVAILABLE"


def test_complete_month_not_invalidated_by_later_missing_month() -> None:
    start = datetime(2024, 3, 31, 23, 59, tzinfo=UTC)
    end = start + timedelta(minutes=2)
    query = replace(declaration(), period_start=start, period_end=end)
    row = kline()
    row[0], row[6] = to_ms(start), to_ms(start) + 59999
    result, months = audit_rows([row], query, as_of=end)
    assert not result.coverage_complete
    assert months[0].utilization == "AVAILABLE" and months[0].coverage_percent == 100
    assert months[1].utilization == "MISSING"


def test_manually_written_authentication_never_accepted(tmp_path: Path) -> None:
    report = file_report(write_csv(tmp_path / "fixture.csv", [kline()]))
    with pytest.raises(ValueError, match="independiente"):
        replace(report.manifest, provider_authenticity=Authentication.AUTHENTICATED)


def test_unlocatable_record_blocks_causal_claim() -> None:
    result, _ = audit_rows([kline(), ["unknown timestamp"]], declaration(), as_of=END)
    assert result.errors and not result.causally_usable
    complete, _ = audit_rows(
        [kline(), kline(1), kline(2)], declaration(), as_of=END + timedelta(microseconds=1)
    )
    assert complete.coverage_complete


def test_json_encoding_crc_and_limits_reject_corruption(tmp_path: Path) -> None:
    path = tmp_path / "fixture.csv"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(UnicodeDecodeError):
        file_report(path)
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("fixture.csv", "unique-content")
    content = path.read_bytes()
    path.write_bytes(content.replace(b"unique-content", b"broken-content"))
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        list(local_rows(path, Limits(), file_format="csv", member="fixture.csv"))
    with pytest.raises(ValueError):
        Limits(max_rows=True)


def test_sqlite_missing_taker_and_gap_never_complete(tmp_path: Path) -> None:
    path = tmp_path / "fixture.sqlite3"
    candle = parse_record(kline(), DatasetType.TRADE, as_of=END)
    assert isinstance(candle, Candle)
    with FuturesSQLite(path) as store:
        store.import_candles(
            [replace(candle, taker_buy_base_volume=None)],
            symbol=DEFAULT_SYMBOL,
            source=Source.REST,
            file="fixture",
            period="fixture",
            as_of=END,
        )
    report = audit_sqlite(path, declaration(), as_of=END)
    assert not report.coverage.complete and report.availability_status == "NOT_AVAILABLE"
