"""CSV + API → SQLite → Candle → estado incremental; 27h causales offline."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, normalize_kline
from trading_engine.market_data.history import Archive, import_archive, sync_range
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.entry import evaluate_entry_checkpoint
from trading_engine.strategy.alt_b.state_market import AltBMarketState, ingest_closed_1m

from .conftest import FakeClient, row_at, zip_fixture


def test_27h_sqlite_reconstruction_matches_all_causal_checkpoints(tmp_path: Path) -> None:
    rows = [row_at(i) for i in range(1620)]
    for i, row in enumerate(rows):
        row[4] = f"100.{i % 19:02d}"
        row[5] = str(i % 11 + 1)
        row[9] = str(Decimal(i % 11 + 1) / 2)
    end = LAUNCH + timedelta(hours=27)
    as_of = end + timedelta(minutes=1)
    original = tuple(normalize_kline(row, DEFAULT_SYMBOL, as_of=as_of) for row in rows)
    archive = Archive(DEFAULT_SYMBOL, "2024-03", Source.MONTHLY)
    path = tmp_path / archive.filename
    checksum = zip_fixture(path, archive, rows[:1600])
    db = tmp_path / "integration.sqlite3"
    with FuturesSQLite(db) as store:
        import_archive(
            store, path, archive, start=LAUNCH, end=end, as_of=as_of, checksum_text=checksum
        )
        report = sync_range(
            store,
            FakeClient(rows),
            DEFAULT_SYMBOL,
            LAUNCH + timedelta(minutes=1600),
            end,
            as_of=as_of,
        )
        assert report.complete and store.bounds(DEFAULT_SYMBOL)[2] == 1620
    config, state = AltBConfig(), AltBMarketState()
    checkpoints = 0
    with FuturesSQLite(db, read_only=True) as store:
        for length, candle in enumerate(store.read(DEFAULT_SYMBOL, LAUNCH, end), start=1):
            assert candle == original[length - 1]
            step = ingest_closed_1m(state, candle, config)
            state = step.state
            assert len(state.candles_1m) <= 1560
            assert all(c.close_time <= candle.close_time for c in state.candles_1m)
            if candle.close_time.minute % 15 == 0:
                checkpoints += 1
                assert step.evaluation == evaluate_entry_checkpoint(
                    original[:length], candle.close_time, config
                )
            else:
                assert step.evaluation is None
    assert checkpoints == 108
    assert state.candles_1m == original[-1560:]
