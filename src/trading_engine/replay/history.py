"""Vista SQLite streaming de solo lectura, consistente durante la sesión offline."""

from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from types import TracebackType

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH
from trading_engine.models.market import Candle
from trading_engine.models.values import Symbol
from trading_engine.persistence.futures_sqlite import FuturesSQLite


class SQLiteHistory:
    def __init__(self, path: Path) -> None:
        self.session = object()
        self.store = FuturesSQLite(path, read_only=True)
        self.store.connection.execute("BEGIN")
        # Ancla el snapshot SQLite antes de leer las velas; sin copiar toda la tabla.
        self.store.connection.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()

    def __enter__(self) -> "SQLiteHistory":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.store.__exit__(kind, error, traceback)

    def read(self, symbol: Symbol, start: datetime, end: datetime) -> Generator[Candle, None, None]:
        previous: Candle | None = None
        for candle in self.store.read(symbol, start, end):
            if symbol == DEFAULT_SYMBOL and candle.open_time < LAUNCH:
                raise ValueError("Vela anterior al lanzamiento del contrato")
            if previous is not None and candle.open_time <= previous.open_time:
                reason = (
                    "duplicate_candle" if candle == previous else "conflicting_or_unordered_candle"
                )
                raise ValueError(reason)
            previous = candle
            yield candle

    def conflicts(self, symbol: Symbol) -> tuple[str, ...]:
        return self.store.conflicts(symbol)
