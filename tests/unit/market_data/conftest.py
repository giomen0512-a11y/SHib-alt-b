"""Fixtures sintéticos offline: nunca se presentan como datos descargados."""

import csv
import hashlib
import io
import json
import zipfile
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from trading_engine.binance.klines import CSV_HEADER, DEFAULT_SYMBOL, LAUNCH, to_ms
from trading_engine.market_data.history import Archive


def row_at(index: int = 0) -> list[object]:
    opened = to_ms(LAUNCH) + index * 60_000
    return [
        opened,
        "100.00000000000000000000000000001",
        "103",
        "99",
        "101",
        "10.00000000000000000000000000001",
        opened + 59999,
        "2000",
        2,
        "4.00000000000000000000000000001",
        "800",
        "0",
    ]


def zip_fixture(
    path: Path,
    archive: Archive,
    rows: list[list[object]],
    *,
    header: bool = True,
    member: str | None = None,
) -> str:
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    if header:
        writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(member or archive.filename.removesuffix(".zip") + ".csv", text.getvalue())
    return f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {archive.filename}\n"


class FakeClient:
    def __init__(self, rows: list[list[object]] | None = None) -> None:
        self.rows = [] if rows is None else rows
        self.calls: list[tuple[str, int, int, int]] = []
        self.downloads: list[str] = []
        self.archives: dict[str, tuple[bytes, str | None]] = {}
        self.missing: set[int] = set()

    def download(self, url: str, target: Path) -> None:
        self.downloads.append(url)
        if url not in self.archives:
            raise HTTPError(url, 404, "missing fixture", Message(), None)
        target.write_bytes(self.archives[url][0])

    def text(self, url: str) -> str:
        checksum = self.archives[url.removesuffix(".CHECKSUM")][1]
        if checksum is None:
            raise HTTPError(url, 404, "checksum missing fixture", Message(), None)
        return checksum

    def klines(
        self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
    ) -> list[list[object]]:
        assert symbol == DEFAULT_SYMBOL.value
        self.calls.append((symbol, start_ms, end_ms, limit))
        return [
            row
            for row in self.rows
            if isinstance(row[0], int)
            and start_ms <= row[0] <= end_ms
            and row[0] not in self.missing
        ][:limit]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        pytest.fail("Los tests de datos deben ser offline")

    monkeypatch.setattr("trading_engine.binance.public_data.open_official", blocked)


@pytest.fixture
def rest_json() -> str:
    return json.dumps([row_at(0), row_at(1)])
