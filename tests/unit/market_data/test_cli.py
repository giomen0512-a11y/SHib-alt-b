import json
from pathlib import Path

import pytest

from trading_engine.binance.klines import DEFAULT_SYMBOL
from trading_engine.market_data.cli import main
from trading_engine.market_data.history import Archive
from trading_engine.persistence.futures_sqlite import Source

from .conftest import row_at, zip_fixture

RANGE = [
    "--start",
    "2024-03-28T07:00:00Z",
    "--end",
    "2024-03-28T07:03:00Z",
    "--as-of",
    "2024-05-01T00:00:00Z",
]


@pytest.mark.parametrize(
    "command", ["plan", "backfill", "sync-once", "recover-range", "audit", "status"]
)
def test_plan_and_dry_run_have_no_network_or_database(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "nonexistent" / "test.sqlite3"
    args = [command, "--db-path", str(path), *RANGE]
    if command != "plan":
        args.append("--dry-run")
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["symbol"] == "1000SHIBUSDC" and "archives" in result
    assert not path.parent.exists()


def test_manual_import_status_audit_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    path = tmp_path / archive.filename
    checksum = zip_fixture(path, archive, [row_at(0), row_at(2)])
    checksum_path = tmp_path / "fixture.CHECKSUM"
    checksum_path.write_text(checksum)
    db = tmp_path / "test.sqlite3"
    assert (
        main(
            [
                "import-archive",
                "--db-path",
                str(db),
                "--archive-file",
                str(path),
                "--source",
                "monthly",
                "--period",
                "2024-03",
                "--checksum-file",
                str(checksum_path),
                *RANGE,
            ]
        )
        == 2
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported["import"]["inserted"] == 2 and not imported["audit"]["complete"]
    assert main(["status", "--db-path", str(db), *RANGE]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["rows"] == 2 and status["first_persisted_open_ms"] == row_at(0)[0]
    assert main(["audit", "--db-path", str(db), *RANGE]) == 2
    assert json.loads(capsys.readouterr().out)["audit"]["missing_minutes"] == 1


def test_status_does_not_create_missing_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "missing.sqlite3"
    assert main(["status", "--db-path", str(path), *RANGE]) == 1
    assert not path.exists()
    assert json.loads(capsys.readouterr().err)["event"] == "data_command_failed"


@pytest.mark.parametrize("argument", ["2024-03-28T07:00:00", "2024-03-28T07:00:00-06:00"])
def test_cli_requires_explicit_utc(argument: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["plan", "--start", argument])
    assert exc.value.code == 2


def test_full_download_requires_explicit_range() -> None:
    with pytest.raises(SystemExit):
        main(["backfill"])
    with pytest.raises(SystemExit):
        main(["sync-once"])


def test_cli_default_clock_with_microseconds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "--start", "2024-03-28T07:00:00Z", "--end", "2024-03-28T07:03:00Z"]) == 0
    assert json.loads(capsys.readouterr().out)["as_of"].endswith("+00:00")
