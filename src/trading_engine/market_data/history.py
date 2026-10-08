"""Importación de archivos oficiales y sincronización manual finita, sin estrategia."""

import csv
import hashlib
import io
import json
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError

from trading_engine.binance.klines import (
    CSV_HEADER,
    DEFAULT_SYMBOL,
    LAUNCH,
    MINUTE_MS,
    from_ms,
    minute_ms,
    normalize_kline,
    reference_ms,
    to_ms,
    validate_symbol,
)
from trading_engine.binance.public_data import ARCHIVE_BASE
from trading_engine.market_data.audit import AuditReport, audit
from trading_engine.models.market import Candle
from trading_engine.models.values import Symbol
from trading_engine.persistence.futures_sqlite import FuturesSQLite, ImportResult, Source

logger = logging.getLogger(__name__)


class PublicDataClient(Protocol):
    def download(self, url: str, target: Path) -> None: ...
    def text(self, url: str) -> str: ...
    def klines(
        self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
    ) -> list[list[object]]: ...


@dataclass(frozen=True)
class Archive:
    symbol: Symbol
    period: str
    source: Source

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if not isinstance(self.source, Source):
            raise TypeError("source debe ser Source")
        if self.source not in (Source.MONTHLY, Source.DAILY):
            raise ValueError("Un archivo debe ser mensual o diario")
        fmt = "%Y-%m" if self.source is Source.MONTHLY else "%Y-%m-%d"
        start = datetime.strptime(self.period, fmt)
        if start.strftime(fmt) != self.period:
            raise ValueError("Período no canónico")

    @property
    def filename(self) -> str:
        return f"{self.symbol.value}-1m-{self.period}.zip"

    @property
    def url(self) -> str:
        return (
            f"{ARCHIVE_BASE}/data/futures/um/{self.source.value}/klines/"
            f"{self.symbol.value}/1m/{self.filename}"
        )

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(
            self.period + ("-01" if self.source is Source.MONTHLY else "")
        ).replace(tzinfo=UTC)

    @property
    def end(self) -> datetime:
        if self.source is Source.DAILY:
            return self.start + timedelta(days=1)
        start = self.start
        return start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)


def validate_range(symbol: Symbol, start: datetime, end: datetime, as_of: datetime) -> None:
    validate_symbol(symbol)
    if minute_ms(start) >= minute_ms(end) or minute_ms(end) > reference_ms(as_of):
        raise ValueError("Rango vacío, invertido o no cerrado")
    if symbol == DEFAULT_SYMBOL and start < LAUNCH:
        raise ValueError("El inicio es anterior al lanzamiento del contrato")


def plan_archives(
    symbol: Symbol, start: datetime, end: datetime, *, as_of: datetime
) -> tuple[Archive, ...]:
    validate_range(symbol, start, end, as_of)
    current = start.replace(day=1, hour=0, minute=0)
    planned: list[Archive] = []
    while current < end:
        monthly = Archive(symbol, current.strftime("%Y-%m"), Source.MONTHLY)
        if monthly.end <= as_of:
            planned.append(monthly)
        else:
            day = max(start, monthly.start).replace(hour=0, minute=0)
            while day < min(end, monthly.end) and day + timedelta(days=1) <= as_of:
                planned.append(Archive(symbol, day.strftime("%Y-%m-%d"), Source.DAILY))
                day += timedelta(days=1)
        current = monthly.end
    return tuple(planned)


def verify_checksum(path: Path, text: str, filename: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) != 1:
        raise ValueError("CHECKSUM debe identificar exactamente un archivo")
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?([^\s]+)", lines[0])
    if not match or match[2] != filename:
        raise ValueError("Formato/nombre CHECKSUM inválido")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != match[1].lower():
        raise ValueError("SHA-256 no coincide con CHECKSUM oficial")
    return actual


def archive_candles(
    path: Path, archive: Archive, *, as_of: datetime, start: datetime, end: datetime
) -> Iterator[Candle]:
    """Un ZIP/CSV por vez, sin extraer archivos ni cargar el histórico en RAM."""
    expected_member = archive.filename.removesuffix(".zip") + ".csv"
    with zipfile.ZipFile(path) as zipped:
        if zipped.namelist() != [expected_member]:
            raise ValueError("ZIP no contiene el CSV Futures esperado para símbolo/período")
        with (
            zipped.open(expected_member) as raw,
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text,
        ):
            previous: datetime | None = None
            found = False
            for index, row in enumerate(csv.reader(text)):
                if index == 0 and tuple(row) == CSV_HEADER:
                    continue
                try:
                    candle = normalize_kline(row, archive.symbol, as_of=as_of)
                    if not archive.start <= candle.open_time < archive.end:
                        raise ValueError("Vela fuera del período del archivo")
                    if previous is not None and candle.open_time <= previous:
                        raise ValueError("CSV duplicado o fuera de orden")
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{archive.filename}: fila {index + 1}: {exc}") from exc
                previous = candle.open_time
                if start <= candle.open_time < end:
                    found = True
                    yield candle
            if not found:
                raise ValueError("CSV no contiene velas en el rango solicitado")


def import_archive(
    store: FuturesSQLite,
    path: Path,
    archive: Archive,
    *,
    as_of: datetime,
    start: datetime,
    end: datetime,
    checksum_text: str | None,
) -> ImportResult:
    validate_range(archive.symbol, start, end, as_of)
    if (
        start < max(archive.start, LAUNCH if archive.symbol == DEFAULT_SYMBOL else archive.start)
        or end > archive.end
    ):
        raise ValueError("Rango de importación fuera del archivo")
    key = f"{archive.filename}#{minute_ms(start)}:{minute_ms(end)}"
    try:
        checksum = (
            None
            if checksum_text is None
            else verify_checksum(path, checksum_text, archive.filename)
        )
        if checksum is None:
            logger.warning(
                json.dumps(
                    {
                        "event": "archive_unverified",
                        "file": archive.filename,
                        "reason": "CHECKSUM ausente",
                    }
                )
            )
    except Exception as exc:
        store.note(key, archive.period, archive.source, "failed", as_of, error=str(exc))
        raise
    return store.import_candles(
        archive_candles(path, archive, as_of=as_of, start=start, end=end),
        symbol=archive.symbol,
        source=archive.source,
        file=key,
        period=archive.period,
        as_of=as_of,
        checksum=checksum,
    )


def download_archive(
    store: FuturesSQLite,
    client: PublicDataClient,
    archive: Archive,
    start: datetime,
    end: datetime,
    as_of: datetime,
) -> bool:
    key = f"{archive.filename}#{minute_ms(start)}:{minute_ms(end)}"
    if store.verified(key):
        return True
    with tempfile.TemporaryDirectory(prefix="shib-futures-") as directory:
        path = Path(directory) / archive.filename
        try:
            client.download(archive.url, path)
            try:
                checksum = client.text(archive.url + ".CHECKSUM")
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                checksum = None
        except HTTPError as exc:
            if exc.code != 404:
                store.note(key, archive.period, archive.source, "failed", as_of, error=str(exc))
                raise
            store.note(key, archive.period, archive.source, "missing", as_of, error="HTTP 404")
            logger.warning(
                json.dumps(
                    {
                        "event": "archive_missing",
                        "file": archive.filename,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    }
                )
            )
            return False
        except Exception as exc:
            store.note(key, archive.period, archive.source, "failed", as_of, error=str(exc))
            raise
        import_archive(
            store, path, archive, start=start, end=end, as_of=as_of, checksum_text=checksum
        )
    return True


def sync_range(
    store: FuturesSQLite,
    client: PublicDataClient,
    symbol: Symbol,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime,
    limit: int = 1500,
) -> AuditReport:
    """Recovery real del rango explícito; los gaps nunca se interpolan."""
    validate_range(symbol, start, end, as_of)
    if type(limit) is not int or not 1 <= limit <= 1500:
        raise ValueError("limit debe estar entre 1 y 1500")
    cursor, end_ms = minute_ms(start), minute_ms(end)
    cutoff = reference_ms(as_of) // MINUTE_MS * MINUTE_MS
    while cursor < end_ms:
        page_end = min(end_ms, cursor + limit * MINUTE_MS)
        key = f"REST:{symbol.value}:{cursor}:{page_end}"
        try:
            rows = client.klines(symbol.value, cursor, page_end - 1, limit)
            if len(rows) > limit:
                raise ValueError("REST excede el límite solicitado")
            candles: list[Candle] = []
            previous = cursor - MINUTE_MS
            for row in rows:
                if len(row) != 12 or type(row[0]) is not int:
                    raise ValueError("REST exige 12 campos y timestamp entero ms")
                opened = row[0]
                if opened < cursor or opened <= previous:
                    raise ValueError("REST fuera de rango, duplicado o fuera de orden")
                previous = opened
                if opened == cutoff and page_end == cutoff:
                    continue  # Excluye el minuto en formación, aunque el proveedor lo devuelva.
                if opened >= page_end:
                    raise ValueError("REST devolvió una vela fuera del rango solicitado")
                candle = normalize_kline(row, symbol, as_of=as_of)
                expected = cursor if not candles else to_ms(candles[-1].close_time)
                if opened != expected:
                    logger.warning(
                        json.dumps(
                            {
                                "event": "rest_gap",
                                "start_ms": expected,
                                "end_ms": opened,
                                "symbol": symbol.value,
                            }
                        )
                    )
                candles.append(candle)
            if not candles:
                store.note(
                    key,
                    f"{cursor}:{page_end}",
                    Source.REST,
                    "missing",
                    as_of,
                    error="REST sin velas cerradas; rango pendiente",
                )
                logger.warning(
                    json.dumps({"event": "rest_empty", "start_ms": cursor, "end_ms": end_ms})
                )
                break
        except Exception as exc:
            store.note(key, f"{cursor}:{page_end}", Source.REST, "failed", as_of, error=str(exc))
            raise
        store.import_candles(
            candles,
            symbol=symbol,
            source=Source.REST,
            file=key,
            period=f"{cursor}:{page_end}",
            as_of=as_of,
        )
        cursor = to_ms(candles[-1].close_time)
    return audit(store, symbol, start, end, as_of=as_of)


def sync_once(
    store: FuturesSQLite,
    client: PublicDataClient,
    symbol: Symbol,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime,
) -> AuditReport:
    """Último minuto + recovery del primer gap previo, para poder reintentar."""
    validate_range(symbol, start, end, as_of)
    _, last, _ = store.bounds(symbol)
    before = audit(store, symbol, start, end, as_of=as_of)
    if before.errors:
        raise ValueError(f"Histórico inválido: {before.errors}")
    next_ms = minute_ms(start) if last is None else max(minute_ms(start), last + MINUTE_MS)
    if before.gaps:
        next_ms = min(next_ms, before.gaps[0].start_ms)
    if next_ms < minute_ms(end):
        sync_range(store, client, symbol, from_ms(next_ms), end, as_of=as_of)
    return audit(store, symbol, start, end, as_of=as_of)


def backfill(
    store: FuturesSQLite,
    client: PublicDataClient,
    symbol: Symbol,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime,
) -> AuditReport:
    for archive in plan_archives(symbol, start, end, as_of=as_of):
        lower, upper = max(start, archive.start), min(end, archive.end)
        if download_archive(store, client, archive, lower, upper, as_of):
            continue
        if archive.source is Source.MONTHLY:
            day = lower.replace(hour=0, minute=0)
            while day < upper:
                daily = Archive(symbol, day.strftime("%Y-%m-%d"), Source.DAILY)
                download_archive(
                    store, client, daily, max(lower, day), min(upper, daily.end), as_of
                )
                day += timedelta(days=1)
    # REST solo para intervalos que siguen ausentes; no repite archivos mensuales verificados.
    report = audit(store, symbol, start, end, as_of=as_of)
    if report.errors:
        raise ValueError(f"Histórico inválido: {report.errors}")
    for gap in report.gaps:
        sync_range(store, client, symbol, from_ms(gap.start_ms), from_ms(gap.end_ms), as_of=as_of)
    return audit(store, symbol, start, end, as_of=as_of)
