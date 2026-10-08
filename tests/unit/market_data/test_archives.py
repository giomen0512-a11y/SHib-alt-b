from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from zipfile import BadZipFile

import pytest

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, normalize_kline, to_ms
from trading_engine.market_data.audit import audit
from trading_engine.market_data.history import (
    Archive,
    backfill,
    download_archive,
    import_archive,
    plan_archives,
    verify_checksum,
)
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source

from .conftest import FakeClient, row_at, zip_fixture

AS_OF = LAUNCH + timedelta(days=40)
END = LAUNCH + timedelta(minutes=3)


@pytest.mark.parametrize(
    "source,period,header",
    [
        (Source.MONTHLY, "2024-03", True),
        (Source.DAILY, "2024-03-28", False),
    ],
)
def test_csv_zip_checksum_and_idempotency(
    tmp_path: Path, source: Source, period: str, header: bool
) -> None:
    archive = Archive(DEFAULT_SYMBOL, period, source)
    path = tmp_path / archive.filename
    checksum = zip_fixture(path, archive, [row_at(i) for i in range(3)], header=header)
    with FuturesSQLite(tmp_path / "test.db") as store:
        for attempt in range(2):
            result = import_archive(
                store, path, archive, as_of=AS_OF, start=LAUNCH, end=END, checksum_text=checksum
            )
            assert result.inserted == (3 if attempt == 0 else 0)
        expected = tuple(normalize_kline(row_at(i), DEFAULT_SYMBOL, as_of=AS_OF) for i in range(3))
        assert tuple(store.read(DEFAULT_SYMBOL, LAUNCH, END)) == expected
        assert store.manifests()[0]["checksum"] == checksum.split()[0]
        assert store.manifests()[0]["source"] == source.value
        assert audit(store, DEFAULT_SYMBOL, LAUNCH, END, as_of=AS_OF).complete


@pytest.mark.parametrize(
    "defect",
    ["checksum", "csv", "zip", "member", "header", "order", "duplicate", "period", "empty"],
)
def test_archive_failures_never_complete_and_no_partial_rows(tmp_path: Path, defect: str) -> None:
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    path = tmp_path / archive.filename
    rows = [row_at(i) for i in range(3)]
    if defect == "csv":
        rows[1][9] = "NaN"
    elif defect == "order":
        rows.reverse()
    elif defect == "duplicate":
        rows[1] = rows[0]
    elif defect == "period":
        rows[1] = row_at(60 * 24 * 6)
    elif defect == "empty":
        rows = []
    elif defect == "header":
        rows.insert(0, ["incorrect"] * 12)
    checksum = zip_fixture(path, archive, rows, member="OTHER.csv" if defect == "member" else None)
    if defect == "checksum":
        checksum = "0" * 64 + "  " + archive.filename
    elif defect == "zip":
        path.write_bytes(b"not a zip")
        checksum = None  # type: ignore[assignment]
    with FuturesSQLite(tmp_path / "test.db") as store:
        with pytest.raises((ValueError, OSError, BadZipFile)):
            import_archive(
                store, path, archive, as_of=AS_OF, start=LAUNCH, end=END, checksum_text=checksum
            )
        assert store.bounds(DEFAULT_SYMBOL)[2] == 0
        assert store.manifests()[0]["state"] == "failed"


@pytest.mark.parametrize(
    "checksum", ["", "a", "0" * 64 + " OTHER.zip", "0" * 64 + " x\n" + "0" * 64 + " y"]
)
def test_invalid_checksum_identification(tmp_path: Path, checksum: str) -> None:
    path = tmp_path / "fixture.zip"
    path.write_bytes(b"fixture")
    with pytest.raises(ValueError):
        verify_checksum(path, checksum, path.name)


def test_missing_archive_recorded_and_checksum_absent_not_verified(tmp_path: Path) -> None:
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    client = FakeClient()
    with FuturesSQLite(tmp_path / "test.db") as store:
        assert not download_archive(store, client, archive, LAUNCH, END, AS_OF)
        assert store.manifests()[0]["state"] == "missing"
        path = tmp_path / archive.filename
        zip_fixture(path, archive, [row_at(i) for i in range(3)])
        client.archives[archive.url] = path.read_bytes(), None
        assert download_archive(store, client, archive, LAUNCH, END, AS_OF)
        manifest = store.manifests()[0]
        assert manifest["state"] == "complete" and manifest["checksum"] is None
        assert not store.verified(str(manifest["file"]))


def test_monthly_preferred_verified_skipped_no_daily_or_rest(tmp_path: Path) -> None:
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    path = tmp_path / archive.filename
    checksum = zip_fixture(path, archive, [row_at(i) for i in range(3)])
    client = FakeClient()
    client.archives[archive.url] = path.read_bytes(), checksum
    with FuturesSQLite(tmp_path / "test.db") as store:
        for _ in range(2):
            assert backfill(store, client, DEFAULT_SYMBOL, LAUNCH, END, as_of=AS_OF).complete
        assert client.downloads == [archive.url]
        assert client.calls == []


def test_monthly_404_falls_back_to_daily_then_rest_gaps(tmp_path: Path) -> None:
    archive = Archive(DEFAULT_SYMBOL, "2024-03-28", Source.DAILY)
    path = tmp_path / archive.filename
    checksum = zip_fixture(path, archive, [row_at(0), row_at(2)])
    client = FakeClient([row_at(1)])
    client.archives[archive.url] = path.read_bytes(), checksum
    with FuturesSQLite(tmp_path / "test.db") as store:
        assert backfill(store, client, DEFAULT_SYMBOL, LAUNCH, END, as_of=AS_OF).complete
        sources = store.connection.execute(
            "SELECT source FROM futures_klines_1m ORDER BY open_time_ms"
        ).fetchall()
        assert sources == [("daily",), ("rest",), ("daily",)]
        assert any(m["state"] == "missing" for m in store.manifests())
        assert client.calls[0][1:3] == (to_ms(LAUNCH) + 60000, to_ms(LAUNCH) + 119999)


def test_plan_completed_month_current_daily_excludes_prelaunch() -> None:
    now = datetime(2024, 4, 3, 12, tzinfo=UTC)
    archives = plan_archives(DEFAULT_SYMBOL, LAUNCH, now, as_of=now)
    assert [(a.period, a.source) for a in archives] == [
        ("2024-03", Source.MONTHLY),
        ("2024-04-01", Source.DAILY),
        ("2024-04-02", Source.DAILY),
    ]
    assert all("/futures/um/" in a.url and "1000SHIBUSDC/1m/" in a.url for a in archives)
    with pytest.raises(ValueError):
        plan_archives(DEFAULT_SYMBOL, LAUNCH - timedelta(minutes=1), now, as_of=now)


@pytest.mark.parametrize(
    "period,source",
    [
        ("2024-3", Source.MONTHLY),
        ("2024-13", Source.MONTHLY),
        ("2024-03-32", Source.DAILY),
        ("2024-03", Source.REST),
    ],
)
def test_invalid_archive_identity(period: str, source: Source) -> None:
    with pytest.raises(ValueError):
        Archive(DEFAULT_SYMBOL, period, source)


def test_non404_http_error_not_reported_as_missing(tmp_path: Path) -> None:
    class Forbidden(FakeClient):
        def download(self, url: str, target: Path) -> None:
            from email.message import Message

            raise HTTPError(url, 451, "blocked", Message(), None)

    with FuturesSQLite(tmp_path / "test.db") as store:
        with pytest.raises(HTTPError):
            download_archive(
                store,
                Forbidden(),
                Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY),
                LAUNCH,
                END,
                AS_OF,
            )
        assert store.manifests()[0]["state"] == "failed"
