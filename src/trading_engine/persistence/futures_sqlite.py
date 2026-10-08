"""Histórico de mercado exacto, transacciones y manifiesto; sin estado financiero."""

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import cast

from trading_engine.binance.klines import (
    DEFAULT_SYMBOL,
    LAUNCH,
    MARKET,
    MINUTE_MS,
    from_ms,
    minute_ms,
    validate_symbol,
)
from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.utils.time import require_utc

logger = logging.getLogger(__name__)


class Source(StrEnum):
    MONTHLY = "monthly"
    DAILY = "daily"
    REST = "rest"


class DataConflict(ValueError):
    """Contenido diferente para una identidad de mercado ya persistida."""


@dataclass(frozen=True)
class ImportResult:
    processed: int
    inserted: int
    duplicates: int


SCHEMA = """
CREATE TABLE IF NOT EXISTS futures_klines_1m (
 market TEXT NOT NULL CHECK(market = 'BINANCE_USDM_FUTURES'),
 symbol TEXT NOT NULL,
 open_time_ms INTEGER NOT NULL CHECK(open_time_ms % 60000 = 0),
 close_time_ms_exclusive INTEGER NOT NULL CHECK(close_time_ms_exclusive = open_time_ms + 60000),
 open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
 base_volume TEXT NOT NULL, taker_buy_base_volume TEXT,
 source TEXT NOT NULL CHECK(source IN ('monthly', 'daily', 'rest')),
 ingested_at_utc TEXT NOT NULL,
 PRIMARY KEY(market, symbol, open_time_ms)
) STRICT;
CREATE TABLE IF NOT EXISTS import_manifest (
 file TEXT PRIMARY KEY, period TEXT NOT NULL, checksum TEXT,
 source TEXT NOT NULL CHECK(source IN ('monthly', 'daily', 'rest')),
 state TEXT NOT NULL CHECK(state IN ('complete', 'failed', 'missing')),
 processed_rows INTEGER NOT NULL, processed_at_utc TEXT NOT NULL,
 error TEXT
) STRICT;
"""
SELECT_CANDLES = """SELECT symbol, open_time_ms, close_time_ms_exclusive,
 open, high, low, close, base_volume, taker_buy_base_volume
 FROM futures_klines_1m WHERE market = ? AND symbol = ?
 AND open_time_ms >= ? AND open_time_ms < ? ORDER BY open_time_ms"""


def _decode(row: tuple[object, ...]) -> Candle:
    symbol, opened, closed, *values = row
    if not isinstance(symbol, str) or type(opened) is not int or type(closed) is not int:
        raise ValueError("Tipos SQLite de mercado inválidos")
    if opened % MINUTE_MS or closed != opened + MINUTE_MS:
        raise ValueError("Intervalo SQLite inválido")
    if any(not isinstance(v, str) for v in values[:5]):
        raise ValueError("OHLC/volumen deben ser TEXT")
    if values[5] is not None and not isinstance(values[5], str):
        raise ValueError("taker-buy debe ser TEXT o NULL")
    parsed = [Decimal(cast(str, v), context=Context()) for v in values[:5]]
    return Candle(
        Symbol(symbol),
        from_ms(opened),
        from_ms(closed),
        Price(parsed[0]),
        Price(parsed[1]),
        Price(parsed[2]),
        Price(parsed[3]),
        parsed[4],
        None if values[5] is None else Decimal(values[5], context=Context()),
    )


class FuturesSQLite:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only:
            self.connection = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=30
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, isolation_level=None, timeout=30)
        try:
            if read_only:
                self.connection.execute("PRAGMA query_only=ON")
            else:
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.executescript(SCHEMA)
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> "FuturesSQLite":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.connection.close()

    def note(
        self,
        file: str,
        period: str,
        source: Source,
        state: str,
        at: datetime,
        *,
        checksum: str | None = None,
        rows: int = 0,
        error: str | None = None,
    ) -> None:
        require_utc(at)
        self.connection.execute(
            """INSERT INTO import_manifest VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file) DO UPDATE SET period=excluded.period, checksum=excluded.checksum,
            source=excluded.source, state=excluded.state, processed_rows=excluded.processed_rows,
            processed_at_utc=excluded.processed_at_utc, error=excluded.error""",
            (
                file,
                period,
                checksum,
                source.value,
                state,
                rows,
                datetime.now(UTC).isoformat(),
                error,
            ),
        )

    def verified(self, file: str) -> bool:
        row = self.connection.execute(
            "SELECT state, checksum FROM import_manifest WHERE file = ?", (file,)
        ).fetchone()
        return row is not None and row[0] == "complete" and row[1] is not None

    def import_candles(
        self,
        candles: Iterable[Candle],
        *,
        symbol: Symbol,
        source: Source,
        file: str,
        period: str,
        as_of: datetime,
        checksum: str | None = None,
    ) -> ImportResult:
        validate_symbol(symbol)
        if not isinstance(source, Source):
            raise TypeError("source debe ser Source")
        at = require_utc(as_of)
        ingested_at = datetime.now(UTC).isoformat()
        processed = inserted = 0
        previous: int | None = None
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for candle in candles:
                if not isinstance(candle, Candle) or candle.symbol != symbol:
                    raise ValueError("Símbolo de procedencia incorrecto")
                if symbol == DEFAULT_SYMBOL and candle.open_time < LAUNCH:
                    raise ValueError("Vela anterior al lanzamiento del contrato")
                opened, closed = minute_ms(candle.open_time), minute_ms(candle.close_time)
                if closed != opened + MINUTE_MS or candle.close_time > at:
                    raise ValueError("Vela no normalizada, abierta o futura")
                if previous is not None and opened <= previous:
                    raise ValueError("Lote duplicado o fuera de orden")
                previous = opened
                existing = tuple(self.candles(symbol, opened, closed))
                if existing:
                    if existing[0] != candle:
                        prior = self.connection.execute(
                            "SELECT source FROM futures_klines_1m WHERE market=? AND symbol=? "
                            "AND open_time_ms=?",
                            (MARKET, symbol.value, opened),
                        ).fetchone()
                        raise DataConflict(
                            f"Conflicto {symbol.value}/{opened}: "
                            f"existente={prior[0]}, entrante={source.value}, archivo={file}"
                        )
                else:
                    self.connection.execute(
                        "INSERT INTO futures_klines_1m VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            MARKET,
                            symbol.value,
                            opened,
                            closed,
                            str(candle.open.value),
                            str(candle.high.value),
                            str(candle.low.value),
                            str(candle.close.value),
                            str(candle.volume),
                            None
                            if candle.taker_buy_base_volume is None
                            else str(candle.taker_buy_base_volume),
                            source.value,
                            ingested_at,
                        ),
                    )
                    inserted += 1
                processed += 1
            if not processed:
                raise ValueError("Importación vacía")
            self.note(file, period, source, "complete", at, checksum=checksum, rows=processed)
            self.connection.execute("COMMIT")
        except BaseException as exc:
            self.connection.execute("ROLLBACK")
            self.note(
                file,
                period,
                source,
                "failed",
                at,
                checksum=checksum,
                rows=processed,
                error=str(exc),
            )
            logger.error(
                json.dumps(
                    {
                        "event": "import_failed",
                        "file": file,
                        "source": source.value,
                        "error": str(exc),
                    }
                )
            )
            raise
        return ImportResult(processed, inserted, processed - inserted)

    def candles(self, symbol: Symbol, start_ms: int, end_ms: int) -> Iterator[Candle]:
        validate_symbol(symbol)
        cursor = self.connection.execute(SELECT_CANDLES, (MARKET, symbol.value, start_ms, end_ms))
        try:
            for row in cursor:
                yield _decode(row)
        finally:
            cursor.close()

    def read(self, symbol: Symbol, start: datetime, end: datetime) -> Iterator[Candle]:
        """Lectura ascendente del intervalo semiabierto [inicio, fin)."""
        return self.candles(symbol, minute_ms(start), minute_ms(end))

    def bounds(self, symbol: Symbol) -> tuple[int | None, int | None, int]:
        validate_symbol(symbol)
        row = self.connection.execute(
            "SELECT min(open_time_ms), max(open_time_ms), count(*) FROM futures_klines_1m "
            "WHERE market=? AND symbol=?",
            (MARKET, symbol.value),
        ).fetchone()
        return cast(tuple[int | None, int | None, int], tuple(row))

    def manifests(self) -> list[dict[str, object]]:
        cursor = self.connection.execute("SELECT * FROM import_manifest ORDER BY file")
        names = [field[0] for field in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor]

    def integrity_errors(self) -> tuple[str, ...]:
        return tuple(
            row[0] for row in self.connection.execute("PRAGMA integrity_check") if row[0] != "ok"
        )

    def conflicts(self, symbol: Symbol) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self.connection.execute(
                "SELECT error FROM import_manifest WHERE state='failed' AND error LIKE ?",
                (f"Conflicto {symbol.value}/%",),
            )
        )
