import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, MARKET, normalize_kline, to_ms
from trading_engine.market_data.audit import audit
from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.persistence.futures_sqlite import DataConflict, FuturesSQLite, Source

from .conftest import row_at

END = LAUNCH + timedelta(minutes=3)
AS_OF = LAUNCH + timedelta(days=1)


def candles() -> tuple[Candle, ...]:
    return tuple(normalize_kline(row_at(i), DEFAULT_SYMBOL, as_of=AS_OF) for i in range(3))


def load(store: FuturesSQLite, rows: tuple[Candle, ...], source: Source = Source.MONTHLY) -> None:
    store.import_candles(
        rows, symbol=DEFAULT_SYMBOL, source=source, file="fixture", period="2024-03", as_of=AS_OF
    )


def test_exact_schema_roundtrip_and_text_storage(tmp_path: Path) -> None:
    path = tmp_path / "futures.sqlite3"
    with FuturesSQLite(path) as store:
        load(store, candles())
        assert tuple(store.read(DEFAULT_SYMBOL, LAUNCH, END)) == candles()
        assert store.bounds(DEFAULT_SYMBOL) == (to_ms(LAUNCH), to_ms(END) - 60000, 3)
        types = store.connection.execute(
            "SELECT market, typeof(open_time_ms), typeof(close_time_ms_exclusive), "
            "typeof(open), typeof(base_volume), typeof(taker_buy_base_volume) "
            "FROM futures_klines_1m"
        ).fetchall()
        assert types == [(MARKET, "integer", "integer", "text", "text", "text")] * 3
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.manifests()[0]["state"] == "complete"
    with pytest.raises(sqlite3.ProgrammingError):
        store.connection.execute("SELECT 1")
    with FuturesSQLite(path, read_only=True) as reopened:
        assert tuple(reopened.read(DEFAULT_SYMBOL, LAUNCH, END)) == candles()
        with pytest.raises(sqlite3.OperationalError):
            reopened.connection.execute("DELETE FROM futures_klines_1m")


def test_idempotent_numeric_equality_preserves_source_and_ingest_time(tmp_path: Path) -> None:
    with FuturesSQLite(tmp_path / "test.db") as store:
        load(store, candles())
        before = store.connection.execute("SELECT * FROM futures_klines_1m").fetchall()
        identical = tuple(
            replace(c, high=Price(Decimal("103.000")), volume=Decimal(str(c.volume)))
            for c in candles()
        )
        result = store.import_candles(
            identical,
            symbol=DEFAULT_SYMBOL,
            source=Source.REST,
            file="api",
            period="range",
            as_of=AS_OF + timedelta(days=1),
        )
        assert result.inserted == 0 and result.duplicates == 3 and result.processed == 3
        assert store.connection.execute("SELECT * FROM futures_klines_1m").fetchall() == before
        assert store.bounds(DEFAULT_SYMBOL)[2] == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", Price(Decimal("100"))),
        ("high", Price(Decimal("104"))),
        ("low", Price(Decimal("98"))),
        ("close", Price(Decimal("102"))),
        ("volume", Decimal("11")),
        ("taker_buy_base_volume", Decimal("4")),
        ("taker_buy_base_volume", None),
    ],
)
def test_conflicts_explicit_and_atomic(
    tmp_path: Path, field: str, value: object, caplog: pytest.LogCaptureFixture
) -> None:
    original = candles()[1]
    revised = replace(original, **{field: value})  # type: ignore[arg-type]
    with FuturesSQLite(tmp_path / "test.db") as store:
        load(store, (original,))
        with pytest.raises(DataConflict, match="existente=monthly, entrante=rest"):
            load(store, (candles()[0], revised), Source.REST)
        assert tuple(store.read(DEFAULT_SYMBOL, LAUNCH, END)) == (original,)
        assert store.manifests()[0]["state"] == "failed"
        assert "import_failed" in caplog.text and "Conflicto" in caplog.text
        report = audit(store, DEFAULT_SYMBOL, LAUNCH, END, as_of=AS_OF)
        assert not report.complete and any("Conflicto" in error for error in report.errors)


def test_interrupted_stream_rollback_and_recovery(tmp_path: Path) -> None:
    def interrupted() -> Iterator[Candle]:
        yield candles()[0]
        raise RuntimeError("stream interrupted")

    with FuturesSQLite(tmp_path / "test.db") as store:
        with pytest.raises(RuntimeError):
            store.import_candles(
                interrupted(),
                symbol=DEFAULT_SYMBOL,
                source=Source.DAILY,
                file="day",
                period="2024-03-28",
                as_of=AS_OF,
            )
        assert store.bounds(DEFAULT_SYMBOL)[2] == 0
        assert store.manifests()[0]["processed_rows"] == 1
        assert store.manifests()[0]["state"] == "failed"
        store.import_candles(
            candles(),
            symbol=DEFAULT_SYMBOL,
            source=Source.DAILY,
            file="day",
            period="2024-03-28",
            as_of=AS_OF,
        )
        assert store.manifests()[0]["state"] == "complete"
        assert audit(store, DEFAULT_SYMBOL, LAUNCH, END, as_of=AS_OF).complete


@pytest.mark.parametrize(
    "defect", ["symbol", "duration", "opened", "duplicate", "reversed", "empty"]
)
def test_invalid_import_rolls_back(tmp_path: Path, defect: str) -> None:
    rows = candles()
    if defect == "symbol":
        rows = (replace(rows[0], symbol=Symbol("OTHER")),)
    elif defect == "duration":
        rows = (replace(rows[0], close_time=rows[0].close_time + timedelta(minutes=1)),)
    elif defect == "opened":
        rows = (replace(rows[0], open_time=AS_OF, close_time=AS_OF + timedelta(minutes=1)),)
    elif defect == "duplicate":
        rows = (rows[0], rows[0])
    elif defect == "reversed":
        rows = rows[::-1]
    else:
        rows = ()
    with FuturesSQLite(tmp_path / "test.db") as store:
        with pytest.raises(ValueError):
            load(store, rows)
        assert store.bounds(DEFAULT_SYMBOL)[2] == 0
        assert store.manifests()[0]["state"] == "failed"


def test_ordered_bounded_read_optional_taker_and_unique_key(tmp_path: Path) -> None:
    rows = candles()
    with FuturesSQLite(tmp_path / "test.db") as store:
        load(store, (rows[2],))
        load(store, (replace(rows[0], taker_buy_base_volume=None),))
        load(store, (rows[1],))
        actual = tuple(store.read(DEFAULT_SYMBOL, LAUNCH, END))
        assert [c.open_time for c in actual] == [c.open_time for c in rows]
        assert actual[0].taker_buy_base_volume is None
        assert tuple(store.read(DEFAULT_SYMBOL, rows[1].open_time, rows[2].open_time)) == (rows[1],)
        assert tuple(store.read(Symbol("OTHER"), LAUNCH, END)) == ()
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO futures_klines_1m SELECT * FROM futures_klines_1m"
            )


def test_audit_leading_internal_trailing_gaps(tmp_path: Path) -> None:
    with FuturesSQLite(tmp_path / "test.db") as store:
        end = LAUNCH + timedelta(minutes=6)
        rows = tuple(normalize_kline(row_at(i), DEFAULT_SYMBOL, as_of=AS_OF) for i in (1, 4))
        load(store, rows)
        report = audit(store, DEFAULT_SYMBOL, LAUNCH, end, as_of=AS_OF)
        assert not report.complete and report.rows == 2
        assert [g.missing_minutes for g in report.gaps] == [1, 2, 1]
        assert report.gaps[1].start_ms == to_ms(LAUNCH) + 120000
        assert not report.errors


def test_audit_empty_and_corrupted_data_never_complete(tmp_path: Path) -> None:
    with FuturesSQLite(tmp_path / "test.db") as store:
        assert not audit(store, DEFAULT_SYMBOL, LAUNCH, END, as_of=AS_OF).complete
        load(store, candles())
        store.connection.execute("UPDATE futures_klines_1m SET high = 'NaN'")
        report = audit(store, DEFAULT_SYMBOL, LAUNCH, END, as_of=AS_OF)
        assert not report.complete and report.errors


def test_audit_rejects_open_or_inverted_range(tmp_path: Path) -> None:
    with FuturesSQLite(tmp_path / "test.db") as store:
        for start, end in ((END, LAUNCH), (LAUNCH, AS_OF + timedelta(minutes=1))):
            with pytest.raises(ValueError):
                audit(store, DEFAULT_SYMBOL, start, end, as_of=AS_OF)
