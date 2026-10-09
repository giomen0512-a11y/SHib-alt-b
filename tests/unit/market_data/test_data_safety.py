import io
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import trading_engine.binance.public_data as http
from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, to_ms
from trading_engine.market_data.cli import main
from trading_engine.market_data.collector import collector_status, run_collection
from trading_engine.persistence.data_safety import (
    IngestionBusy,
    consistent_backup,
    exclusive_database,
    require_space,
)
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source

from .conftest import FakeClient
from .test_collector import at_minute, config_at, seed_ready


def test_all_writers_use_same_database_lock_and_aliases(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    with FuturesSQLite(path):
        with pytest.raises(IngestionBusy):
            FuturesSQLite(path)
        alias = tmp_path / "alias.sqlite3"
        alias.symlink_to(path)
        with pytest.raises(IngestionBusy):
            FuturesSQLite(alias)
        with FuturesSQLite(path, read_only=True) as reader:
            assert reader.bounds(DEFAULT_SYMBOL)[2] == 0
    with FuturesSQLite(path):
        pass
    assert Path(str(path) + ".lock").exists()


def test_lock_released_after_error_without_deleting_inode(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    with pytest.raises(RuntimeError), exclusive_database(path):
        raise RuntimeError("fixture")
    with exclusive_database(path):
        pass


def test_symlink_lockfile_rejected(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    target = tmp_path / "unrelated"
    target.write_text("keep")
    Path(str(path) + ".lock").symlink_to(target)
    with pytest.raises(OSError), exclusive_database(path):
        pass
    assert target.read_text() == "keep"


def test_two_real_processes_cannot_ingest_same_database(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    code = """
import sys
from pathlib import Path
from trading_engine.persistence.futures_sqlite import FuturesSQLite
with FuturesSQLite(Path(sys.argv[1])):
    print('LOCKED', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "LOCKED"
        with pytest.raises(IngestionBusy):
            FuturesSQLite(path)
        assert main(["history-run", "--db-path", str(path), "--min-free-mib", "0"]) == 73
    finally:
        process.kill()
        process.communicate(timeout=10)
    with FuturesSQLite(path):
        pass


def test_process_crash_during_transaction_rolls_back_and_releases_lock(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite3"
    code = """
import sys
from pathlib import Path
from trading_engine.persistence.futures_sqlite import FuturesSQLite
with FuturesSQLite(Path(sys.argv[1])) as store:
    store.connection.execute('BEGIN IMMEDIATE')
    store.connection.execute("INSERT INTO import_manifest VALUES ('interrupted', '2024-03', NULL, "
                             "'monthly', 'complete', 1, '2024-03-29T00:00:00+00:00', NULL)")
    print('TRANSACTION_OPEN', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert (
            process.stdout is not None and process.stdout.readline().strip() == "TRANSACTION_OPEN"
        )
    finally:
        process.kill()
        process.communicate(timeout=10)
    with FuturesSQLite(path) as store:
        assert store.manifests() == [] and not store.integrity_errors()


def test_sigkill_between_market_commit_and_checkpoint_resumes_from_sqlite(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    code = """
import sys
from pathlib import Path
from datetime import timedelta
import trading_engine.market_data.collector as collector
from trading_engine.binance.klines import LAUNCH, to_ms
from trading_engine.market_data.collector import CollectorConfig, run_collection
class Offline:
    def klines(self, symbol, start_ms, end_ms, limit=1500):
        return [[to_ms(LAUNCH)+i*60000, '100', '103', '99', '101', '10',
                 to_ms(LAUNCH)+i*60000+59999, '2000', 2, '4', '800', '0'] for i in (3,4)]
    def download(self, *args):
        raise AssertionError('offline test')
    def text(self, *args):
        raise AssertionError('offline test')
def stop_after_commit(*args, **kwargs):
    print('MARKET_COMMITTED', flush=True)
    sys.stdin.readline()
collector.record_audit = stop_after_commit
run_collection(CollectorConfig(Path(sys.argv[1]), min_free_bytes=0), 'update',
               client=Offline(), as_of=LAUNCH+timedelta(minutes=5, seconds=5))
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(config.db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert (
            process.stdout is not None and process.stdout.readline().strip() == "MARKET_COMMITTED"
        )
    finally:
        process.kill()
        process.communicate(timeout=10)
    status = collector_status(config, as_of=at_minute(5))
    assert status["rows"] == 5
    client = FakeClient()
    assert run_collection(config, "update", client=client, as_of=at_minute(5))["complete"]
    assert not client.calls


def test_disk_reserve_prevents_database_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "trading_engine.persistence.data_safety.shutil.disk_usage",
        lambda path: SimpleNamespace(free=100),
    )
    config = config_at(tmp_path, min_free_bytes=101)
    with pytest.raises(OSError, match="Espacio"):
        run_collection(config, "history", client=FakeClient(), as_of=at_minute(3))
    assert not config.db_path.exists()
    with pytest.raises(ValueError):
        require_space(tmp_path, -1)


def test_backup_is_consistent_includes_wal_and_does_not_overwrite(tmp_path: Path) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    target = tmp_path / "backup.sqlite3"
    with FuturesSQLite(config.db_path) as store:
        # Una escritura reciente en WAL, conservando el prefijo financiero.
        store.note("new_manifest", "period", Source.REST, "complete", at_minute(3))
        assert Path(str(config.db_path) + "-wal").exists()
        consistent_backup(store, target, reserve=0)
        expected = tuple(store.read(DEFAULT_SYMBOL, LAUNCH, at_minute(3).replace(second=0)))
        with pytest.raises(FileExistsError):
            consistent_backup(store, target, reserve=0)
        with pytest.raises(FileExistsError):
            consistent_backup(store, config.db_path, reserve=0)
    with FuturesSQLite(target, read_only=True) as copied:
        assert (
            tuple(copied.read(DEFAULT_SYMBOL, LAUNCH, at_minute(3).replace(second=0))) == expected
        )
        assert copied.connection.execute(
            "SELECT file FROM import_manifest WHERE file='new_manifest'"
        ).fetchone()
        assert not copied.integrity_errors()


def test_failed_backup_connection_leaves_no_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    target = tmp_path / "backup.sqlite3"
    with FuturesSQLite(config.db_path) as store:

        def fail(*args: object, **kwargs: object) -> sqlite3.Connection:
            raise OSError("fixture backup connect failure")

        monkeypatch.setattr("trading_engine.persistence.data_safety.sqlite3.connect", fail)
        with pytest.raises(OSError):
            consistent_backup(store, target, reserve=0)
        assert store.bounds(DEFAULT_SYMBOL)[2] == 3
    assert not target.exists() and not tuple(tmp_path.glob("backup.sqlite3.*.part"))


def test_backup_checks_destination_space_for_full_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_at(tmp_path)
    seed_ready(config)
    with FuturesSQLite(config.db_path) as store:
        monkeypatch.setattr(
            "trading_engine.persistence.data_safety.shutil.disk_usage",
            lambda path: SimpleNamespace(free=1),
        )
        with pytest.raises(OSError):
            consistent_backup(store, tmp_path / "backup.sqlite3", reserve=0)


def test_http_pacing_applies_between_public_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(io.BytesIO):
        def geturl(self) -> str:
            return http.REST_BASE

    waits: list[int] = []
    monkeypatch.setattr("trading_engine.binance.public_data.time.monotonic", lambda: 0)
    monkeypatch.setattr(http, "open_official", lambda request, timeout: Response(b"[]"))
    client = http.PublicHTTP(request_interval_seconds=2, sleeper=waits.append)
    for _ in range(2):
        assert client.klines(DEFAULT_SYMBOL.value, to_ms(LAUNCH), to_ms(LAUNCH) + 59999) == []
    assert waits == [2]
