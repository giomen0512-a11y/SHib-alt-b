"""Simulación offline de operación, reinicio y recovery; no datos reales."""

import sqlite3
import ssl
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import TypedDict
from urllib.error import HTTPError, URLError

import pytest

import trading_engine.market_data.cli as cli_module
import trading_engine.market_data.collector as collector
from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, normalize_kline, to_ms
from trading_engine.market_data.cli import main
from trading_engine.market_data.collector import (
    CollectorConfig,
    collector_status,
    eligible_end,
    run_collection,
)
from trading_engine.market_data.history import Archive
from trading_engine.persistence.collector_progress import load_progress, record_audit
from trading_engine.persistence.data_safety import consistent_backup
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source

from .conftest import FakeClient, row_at, zip_fixture


def config_at(tmp_path: Path, **changes: int) -> CollectorConfig:
    return CollectorConfig(
        tmp_path / "history.sqlite3",
        publication_delay_seconds=changes.get("publication_delay_seconds", 5),
        max_update_minutes=changes.get("max_update_minutes", 1500),
        min_free_bytes=changes.get("min_free_bytes", 0),
        request_interval_seconds=changes.get("request_interval_seconds", 1),
    )


class RunTimes(TypedDict):
    as_of: datetime
    end: datetime


def at_minute(minutes: int) -> datetime:
    return LAUNCH + timedelta(minutes=minutes, seconds=5)


def seed_ready(config: CollectorConfig, minutes: int = 3) -> None:
    with FuturesSQLite(config.db_path) as store:
        store.import_candles(
            (
                normalize_kline(row_at(i), DEFAULT_SYMBOL, as_of=at_minute(minutes))
                for i in range(minutes)
            ),
            symbol=DEFAULT_SYMBOL,
            source=Source.MONTHLY,
            file="existing_march",
            period="2024-03",
            as_of=at_minute(minutes),
        )
    assert run_collection(config, "audit", as_of=at_minute(minutes))["complete"]


def test_adopts_existing_phase1_history_without_replacing_candles(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    with FuturesSQLite(config.db_path) as store:
        store.import_candles(
            (normalize_kline(row_at(i), DEFAULT_SYMBOL, as_of=at_minute(3)) for i in range(3)),
            symbol=DEFAULT_SYMBOL,
            source=Source.MONTHLY,
            file="march",
            period="2024-03",
            as_of=at_minute(3),
        )
        before = store.connection.execute("SELECT * FROM futures_klines_1m").fetchall()
    assert run_collection(config, "audit", as_of=at_minute(3))["complete"]
    with FuturesSQLite(config.db_path, read_only=True) as store:
        assert store.connection.execute("SELECT * FROM futures_klines_1m").fetchall() == before
    assert collector_status(config, as_of=at_minute(3))["last_successful_sync_utc"] is None


def test_update_requires_full_history_validation_before_timer(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    with FuturesSQLite(config.db_path):
        pass
    with pytest.raises(ValueError, match="sin validar"):
        run_collection(config, "update", client=FakeClient(), as_of=at_minute(3))
    status = collector_status(config, as_of=at_minute(3))
    assert not status["complete"] and status["pending_errors"]


def test_update_only_new_minutes_and_repeated_invocation_no_rest(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    client = FakeClient([row_at(i) for i in range(5)])
    assert run_collection(config, "update", client=client, as_of=at_minute(5))["complete"]
    assert client.calls == [
        (DEFAULT_SYMBOL.value, to_ms(LAUNCH) + 180000, to_ms(LAUNCH) + 299999, 1500)
    ]
    status = collector_status(config, as_of=at_minute(5))
    assert status["rows"] == 5 and status["lag_minutes"] == 0
    assert status["last_successful_sync_utc"] and not status["pending_errors"]
    run_collection(config, "update", client=client, as_of=at_minute(5))
    assert len(client.calls) == 1


def test_update_queries_are_bounded_and_no_full_integrity_or_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    statements: list[str] = []
    connect = FuturesSQLite._connect

    def traced(self: FuturesSQLite, path: Path, *, read_only: bool) -> None:
        connect(self, path, read_only=read_only)
        self.connection.set_trace_callback(statements.append)

    monkeypatch.setattr(FuturesSQLite, "_connect", traced)
    run_collection(config, "update", client=FakeClient([row_at(3)]), as_of=at_minute(4))
    sql = "\n".join(statements).lower()
    assert "integrity_check" not in sql and "count(*)" not in sql
    assert "order by open_time_ms desc limit 1" in sql
    candle_queries = [s for s in statements if s.startswith("SELECT symbol, open_time_ms")]
    assert candle_queries and all("1711609200000" not in s for s in candle_queries)


def test_long_disconnect_processed_in_bounded_runs_and_restarts(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    client = FakeClient([row_at(i) for i in range(3005)])
    first = run_collection(config, "update", client=client, as_of=at_minute(3005))
    assert not first["complete"]
    assert collector_status(config, as_of=at_minute(3005))["rows"] == 1503
    # Estado se reconstruye de SQLite en cada invocación, sin cursor en memoria.
    second = run_collection(replace(config), "update", client=client, as_of=at_minute(3005))
    assert not second["complete"]
    third = run_collection(replace(config), "update", client=client, as_of=at_minute(3005))
    assert third["complete"]
    assert len(client.calls) == 3 and all(call[3] == 1500 for call in client.calls)
    assert collector_status(config, as_of=at_minute(3005))["rows"] == 3005


def test_multi_minute_gap_persists_and_retries_first_missing_not_last_row(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    client = FakeClient([row_at(i) for i in range(10)])
    client.missing = {to_ms(LAUNCH) + i * 60000 for i in (3, 4, 5)}
    assert not run_collection(config, "update", client=client, as_of=at_minute(10))["complete"]
    status = collector_status(config, as_of=at_minute(10))
    assert status["rows"] == 7 and status["minutes_missing_verified"] == 3
    client.missing.clear()
    assert run_collection(config, "update", client=client, as_of=at_minute(10))["complete"]
    assert client.calls[-1][1] == to_ms(LAUNCH) + 3 * 60000
    assert client.calls[-1][2] == to_ms(LAUNCH) + 6 * 60000 - 1
    assert collector_status(config, as_of=at_minute(10))["rows"] == 10


def test_publication_delay_and_open_minutes_excluded(tmp_path: Path) -> None:
    config = config_at(tmp_path, publication_delay_seconds=10)
    # Auditoría semilla usa el mismo cutoff, 07:03:00 a las 07:03:10.
    with FuturesSQLite(config.db_path) as store:
        store.import_candles(
            (normalize_kline(row_at(i), DEFAULT_SYMBOL, as_of=at_minute(4)) for i in range(3)),
            symbol=DEFAULT_SYMBOL,
            source=Source.REST,
            file="seed",
            period="seed",
            as_of=at_minute(4),
        )
    run_collection(config, "audit", as_of=LAUNCH + timedelta(minutes=3, seconds=10))
    client = FakeClient([row_at(i) for i in range(6)])
    now = LAUNCH + timedelta(minutes=5, seconds=9, microseconds=999999)
    assert eligible_end(config, now) == LAUNCH + timedelta(minutes=4)
    run_collection(config, "update", client=client, as_of=now)
    assert collector_status(config, as_of=now)["rows"] == 4
    assert client.calls[0][2] == to_ms(LAUNCH) + 4 * 60000 - 1


def test_empty_rest_records_pending_tail_and_visible_attempt(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    result = run_collection(config, "update", client=FakeClient(), as_of=at_minute(5))
    assert not result["complete"]
    status = collector_status(config, as_of=at_minute(5))
    assert status["minutes_missing_verified"] == 2 and status["last_attempt_utc"]
    assert status["last_import"] and status["last_successful_sync_utc"] is None


def test_periodic_audit_of_disconnect_does_not_disable_recovery(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    assert not run_collection(config, "audit", as_of=at_minute(6))["complete"]
    client = FakeClient([row_at(i) for i in range(3, 6)])
    assert run_collection(config, "update", client=client, as_of=at_minute(6))["complete"]


@pytest.mark.parametrize("corruption", ["ohlc", "taker", "delete"])
def test_data_corruption_invalidates_gate_and_full_audit_reports(
    tmp_path: Path, corruption: str
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    with FuturesSQLite(config.db_path) as store:
        if corruption == "ohlc":
            store.connection.execute("UPDATE futures_klines_1m SET high='NaN'")
        elif corruption == "taker":
            store.connection.execute("UPDATE futures_klines_1m SET taker_buy_base_volume=NULL")
        else:
            store.connection.execute(
                "DELETE FROM futures_klines_1m WHERE open_time_ms=?", (to_ms(LAUNCH),)
            )
    with pytest.raises(ValueError, match="sin validar"):
        run_collection(config, "update", client=FakeClient(), as_of=at_minute(4))
    if corruption == "delete":
        assert not run_collection(config, "audit", as_of=at_minute(4))["complete"]
    else:
        with pytest.raises(ValueError):
            run_collection(config, "audit", as_of=at_minute(4))
    assert not collector_status(config, as_of=at_minute(4))["complete"]


def test_invalid_sqlite_file_fails_without_replacement(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    config.db_path.write_bytes(b"corrupt fixture, not SQLite")
    with pytest.raises(sqlite3.DatabaseError):
        run_collection(config, "audit", as_of=at_minute(3))
    assert config.db_path.read_bytes() == b"corrupt fixture, not SQLite"


@pytest.mark.parametrize("code", [429, 403, 451, 500, 503])
def test_http_failure_visible_and_no_data_mutation(
    tmp_path: Path, code: int, caplog: pytest.LogCaptureFixture
) -> None:
    class Failing(FakeClient):
        def klines(
            self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
        ) -> list[list[object]]:
            raise HTTPError(
                "https://fapi.binance.com/fapi/v1/klines", code, "fixture", Message(), None
            )

    config = config_at(tmp_path)
    seed_ready(config)
    with pytest.raises(HTTPError):
        run_collection(config, "update", client=Failing(), as_of=at_minute(4))
    status = collector_status(config, as_of=at_minute(4))
    assert status["rows"] == 3 and status["pending_errors"]
    assert "collector_failed" in caplog.text


def test_history_reuses_checksum_verified_month_and_existing_rows(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    file = tmp_path / archive.filename
    checksum = zip_fixture(file, archive, [row_at(i) for i in range(3)])
    client = FakeClient()
    client.archives[archive.url] = file.read_bytes(), checksum
    now = datetime(2024, 5, 1, tzinfo=UTC)
    through = LAUNCH + timedelta(minutes=3)
    for _ in range(2):
        result = run_collection(config, "history", client=client, as_of=now, end=through)
        assert not result["complete"]  # Solo intervalo de test; no todo el histórico hasta mayo.
    assert client.downloads == [archive.url] and not client.calls
    assert collector_status(config, as_of=now)["rows"] == 3


def test_checksum_required_before_data2_archive_commit(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    file = tmp_path / archive.filename
    zip_fixture(file, archive, [row_at(i) for i in range(3)])
    client = FakeClient()
    client.archives[archive.url] = file.read_bytes(), None
    with pytest.raises(ValueError, match="CHECKSUM"):
        run_collection(
            config,
            "history",
            client=client,
            as_of=datetime(2024, 5, 1, tzinfo=UTC),
            end=LAUNCH + timedelta(minutes=3),
        )
    assert collector_status(config, as_of=at_minute(3))["rows"] == 0


def test_history_archive_error_recovered_on_repeat(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    file = tmp_path / archive.filename
    checksum = zip_fixture(file, archive, [row_at(0), row_at(1), row_at(2)])
    client = FakeClient()
    client.archives[archive.url] = b"interrupted zip fixture", checksum
    args: RunTimes = {
        "as_of": datetime(2024, 5, 1, tzinfo=UTC),
        "end": LAUNCH + timedelta(minutes=3),
    }
    with pytest.raises(ValueError, match="SHA-256"):
        run_collection(config, "history", client=client, **args)
    client.archives[archive.url] = file.read_bytes(), checksum
    run_collection(config, "history", client=client, **args)
    assert collector_status(config, as_of=at_minute(3))["rows"] == 3


def test_history_multiple_periods_and_resume_completed_first_month(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    april = datetime(2024, 4, 1, tzinfo=UTC)
    count = (to_ms(april) - to_ms(LAUNCH)) // 60000
    march_archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    april_archive = Archive(DEFAULT_SYMBOL, "2024-04", Source.MONTHLY)
    client = FakeClient()
    path = tmp_path / march_archive.filename
    checksum = zip_fixture(path, march_archive, [row_at(i) for i in range(count)])
    client.archives[march_archive.url] = path.read_bytes(), checksum

    # Segunda descarga falla; primera transacción/checksum ya están confirmados.
    class Interrupted(FakeClient):
        def download(self, url: str, target: Path) -> None:
            if url == april_archive.url:
                raise TimeoutError("fixture interrupted between months")
            super().download(url, target)

    interrupted = Interrupted()
    interrupted.archives = client.archives.copy()
    args: RunTimes = {
        "as_of": datetime(2024, 6, 1, tzinfo=UTC),
        "end": april + timedelta(minutes=2),
    }
    with pytest.raises(TimeoutError):
        run_collection(config, "history", client=interrupted, **args)
    assert collector_status(config, as_of=at_minute(count))["rows"] == count
    path = tmp_path / april_archive.filename
    checksum = zip_fixture(path, april_archive, [row_at(count), row_at(count + 1)])
    client.archives[april_archive.url] = path.read_bytes(), checksum
    run_collection(config, "history", client=client, **args)
    assert client.downloads == [april_archive.url]
    assert collector_status(config, as_of=at_minute(count + 2))["rows"] == count + 2


def test_collector_status_read_only_and_no_network(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    before = config.db_path.read_bytes()
    status = collector_status(config, as_of=at_minute(5))
    assert status["unverified_minutes"] == 2 and status["lag_minutes"] == 2
    assert status["first_open_utc"] == LAUNCH.isoformat() and status["free_bytes"]
    assert not status["complete"] and config.db_path.read_bytes() == before


def test_backup_failure_is_visible_and_last_sync_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    run_collection(config, "update", client=FakeClient(), as_of=at_minute(3))
    last_success = collector_status(config, as_of=at_minute(3))["last_successful_sync_utc"]

    def failing(*args: object, **kwargs: object) -> Path:
        raise OSError("backup storage fixture error")

    monkeypatch.setattr(collector, "consistent_backup", failing)
    with pytest.raises(OSError):
        run_collection(
            config, "backup", as_of=at_minute(3), backup_path=tmp_path / "backup.sqlite3"
        )
    status = collector_status(config, as_of=at_minute(3))
    assert status["rows"] == 3 and status["last_successful_sync_utc"] == last_success
    assert "backup storage" in str(status["pending_errors"])


def test_failed_backup_remains_visible_after_update_until_backup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    original = consistent_backup

    def failing(*args: object, **kwargs: object) -> Path:
        raise OSError("backup storage unavailable")

    monkeypatch.setattr(collector, "consistent_backup", failing)
    with pytest.raises(OSError):
        run_collection(config, "backup", as_of=at_minute(3), backup_path=tmp_path / "copy.db")
    run_collection(config, "update", client=FakeClient([row_at(3)]), as_of=at_minute(4))
    status = collector_status(config, as_of=at_minute(4))
    assert status["rows"] == 4 and not status["complete"]
    assert "backup storage unavailable" in str(status["pending_errors"])
    monkeypatch.setattr(collector, "consistent_backup", original)
    run_collection(config, "backup", as_of=at_minute(4), backup_path=tmp_path / "copy.db")
    assert not collector_status(config, as_of=at_minute(4))["pending_errors"]


def test_status_reads_unmigrated_phase1_database_without_writing(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    with sqlite3.connect(config.db_path) as connection:
        for table in ("collector_state", "collector_gaps", "collector_errors"):
            connection.execute(f"DROP TABLE {table}")
    before = config.db_path.read_bytes()
    status = collector_status(config, as_of=at_minute(3))
    assert status["rows"] == 3 and status["progress"] is None and not status["complete"]
    assert config.db_path.read_bytes() == before


def test_error_remains_visible_if_disk_prevents_recording_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)

    class FailedClient(FakeClient):
        def klines(
            self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
        ) -> list[list[object]]:
            raise TimeoutError("original transport timeout")

    def no_storage(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(collector, "finish_error", no_storage)
    with pytest.raises(TimeoutError, match="original transport"):
        run_collection(config, "update", client=FailedClient(), as_of=at_minute(4))
    assert "original transport timeout" in caplog.text
    assert "collector_error_record_failed" in caplog.text and "disk is full" in caplog.text


@pytest.mark.parametrize(
    "error,expected",
    [
        (HTTPError("https://fapi.binance.com", 429, "fixture", Message(), None), 75),
        (HTTPError("https://fapi.binance.com", 503, "fixture", Message(), None), 75),
        (HTTPError("https://fapi.binance.com", 403, "fixture", Message(), None), 1),
        (HTTPError("https://fapi.binance.com", 451, "fixture", Message(), None), 1),
        (URLError("Tunnel connection failed: 403 Forbidden"), 1),
        (URLError("Tunnel connection failed: 451 Unavailable"), 1),
        (URLError(ssl.SSLCertVerificationError("fixture invalid certificate")), 1),
        (TimeoutError("fixture"), 75),
    ],
)
def test_cli_classifies_transient_and_permanent_network_failures(
    error: Exception, expected: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing(*args: object, **kwargs: object) -> dict[str, object]:
        raise error

    monkeypatch.setattr(cli_module, "run_collection", failing)
    assert main(["history-run", "--db-path", str(tmp_path / "fixture.db")]) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("publication_delay_seconds", -1),
        ("publication_delay_seconds", True),
        ("publication_delay_seconds", 301),
        ("max_update_minutes", 0),
        ("max_update_minutes", 10081),
        ("request_interval_seconds", 0),
        ("min_free_bytes", -1),
    ],
)
def test_config_rejects_invalid_limits(tmp_path: Path, field: str, value: int) -> None:
    with pytest.raises(ValueError):
        config_at(tmp_path, **{field: value})


def test_update_clock_regression_fails_closed(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    with pytest.raises(ValueError, match="Reloj"):
        run_collection(config, "update", client=FakeClient(), as_of=at_minute(2))


@pytest.mark.parametrize(
    "command", ["history-run", "update-once", "audit-full", "collector-status", "backup"]
)
def test_new_cli_dry_run_creates_no_data(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "not_created" / "test.sqlite3"
    args = [command, "--db-path", str(db), "--dry-run"]
    if command == "backup":
        args += ["--backup-path", str(tmp_path / "backup.sqlite3")]
    assert main(args) == 0
    assert '"dry_run": true' in capsys.readouterr().out and not db.parent.exists()


@pytest.mark.parametrize(
    "command,expected",
    [("audit-full", 0), ("update-once", 0), ("collector-status", 0), ("backup", 0)],
)
def test_new_cli_runs_offline_existing_ready_database(
    command: str, expected: int, tmp_path: Path
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    args = [
        command,
        "--db-path",
        str(config.db_path),
        "--min-free-mib",
        "0",
        "--as-of",
        at_minute(3).isoformat(),
    ]
    if command == "backup":
        args += ["--backup-path", str(tmp_path / "backup.sqlite3")]
    assert main(args) == expected


def test_progress_prefix_persists_after_unexpected_checkpoint_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    original = record_audit

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt("fixture after market COMMIT, before checkpoint")

    monkeypatch.setattr(collector, "record_audit", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_collection(
            config, "update", client=FakeClient([row_at(3), row_at(4)]), as_of=at_minute(5)
        )
    with FuturesSQLite(config.db_path, read_only=True) as store:
        progress = load_progress(store, DEFAULT_SYMBOL)
        assert progress is not None and progress.validated_end_ms == to_ms(LAUNCH) + 180000
        assert store.bounds(DEFAULT_SYMBOL)[2] == 5
    monkeypatch.setattr(collector, "record_audit", original)
    client = FakeClient()
    assert run_collection(config, "update", client=client, as_of=at_minute(5))["complete"]
    assert not client.calls
