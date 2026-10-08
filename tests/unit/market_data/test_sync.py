from datetime import timedelta
from pathlib import Path

import pytest

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, to_ms
from trading_engine.market_data.history import sync_once, sync_range
from trading_engine.persistence.futures_sqlite import FuturesSQLite

from .conftest import FakeClient, row_at

AS_OF = LAUNCH + timedelta(days=1)


def test_paginated_rest_and_repeated_sync_unchanged(tmp_path: Path) -> None:
    client = FakeClient([row_at(i) for i in range(7)])
    end = LAUNCH + timedelta(minutes=7)
    with FuturesSQLite(tmp_path / "test.db") as store:
        assert sync_range(store, client, DEFAULT_SYMBOL, LAUNCH, end, as_of=AS_OF, limit=3).complete
        assert len(client.calls) == 3
        assert [call[1] for call in client.calls] == [to_ms(LAUNCH) + i * 60000 for i in (0, 3, 6)]
        assert [call[2] for call in client.calls] == [
            to_ms(LAUNCH) + i * 60000 - 1 for i in (3, 6, 7)
        ]
        before = list(store.connection.iterdump())
        assert sync_once(store, client, DEFAULT_SYMBOL, LAUNCH, end, as_of=AS_OF).complete
        assert list(store.connection.iterdump()) == before
        assert len(client.calls) == 3


def test_official_limit_1500_supported(tmp_path: Path) -> None:
    client = FakeClient([row_at(i) for i in range(1501)])
    end = LAUNCH + timedelta(minutes=1501)
    with FuturesSQLite(tmp_path / "test.db") as store:
        assert sync_range(
            store, client, DEFAULT_SYMBOL, LAUNCH, end, as_of=end + timedelta(minutes=1)
        ).complete
        assert len(client.calls) == 2 and all(call[3] == 1500 for call in client.calls)
        assert store.bounds(DEFAULT_SYMBOL)[2] == 1501


def test_missing_minutes_reported_then_recovered_by_real_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient([row_at(i) for i in range(5)])
    client.missing = {to_ms(LAUNCH), to_ms(LAUNCH) + 120000}
    end = LAUNCH + timedelta(minutes=5)
    with FuturesSQLite(tmp_path / "test.db") as store:
        report = sync_once(store, client, DEFAULT_SYMBOL, LAUNCH, end, as_of=AS_OF)
        assert not report.complete and len(report.gaps) == 2
        assert store.bounds(DEFAULT_SYMBOL)[2] == 3
        assert "rest_gap" in caplog.text
        client.missing.clear()
        assert sync_once(store, client, DEFAULT_SYMBOL, LAUNCH, end, as_of=AS_OF).complete
        assert client.calls[-1][1] == to_ms(LAUNCH)
        assert store.bounds(DEFAULT_SYMBOL)[2] == 5


def test_empty_and_truncated_responses_leave_retryable_tail(tmp_path: Path) -> None:
    client = FakeClient([row_at(0)])
    end = LAUNCH + timedelta(minutes=3)
    with FuturesSQLite(tmp_path / "test.db") as store:
        report = sync_once(store, client, DEFAULT_SYMBOL, LAUNCH, end, as_of=AS_OF)
        assert not report.complete and report.gaps[0].missing_minutes == 2
        assert any(m["state"] == "missing" for m in store.manifests())
        client.rows.extend([row_at(1), row_at(2)])
        assert sync_once(store, client, DEFAULT_SYMBOL, LAUNCH, end, as_of=AS_OF).complete
        assert client.calls[-1][1] == to_ms(LAUNCH) + 60000


@pytest.mark.parametrize(
    "defect",
    ["reversed", "duplicate", "prior", "future", "microseconds", "malformed", "over_limit"],
)
def test_invalid_rest_batch_atomic_failure(tmp_path: Path, defect: str) -> None:
    class Broken(FakeClient):
        def klines(
            self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
        ) -> list[list[object]]:
            rows = [row_at(0), row_at(1)]
            if defect == "reversed":
                rows.reverse()
            elif defect == "duplicate":
                rows[1] = rows[0]
            elif defect == "prior":
                rows[0] = row_at(-1)
            elif defect == "future":
                rows[1] = row_at(4)
            elif defect == "microseconds":
                rows[1][0] = to_ms(LAUNCH) * 1000
            elif defect == "malformed":
                rows[1] = rows[1][:11]
            else:
                rows *= 800
            return rows

    with FuturesSQLite(tmp_path / "test.db") as store:
        with pytest.raises(ValueError):
            sync_range(
                store, Broken(), DEFAULT_SYMBOL, LAUNCH, LAUNCH + timedelta(minutes=3), as_of=AS_OF
            )
        assert store.bounds(DEFAULT_SYMBOL)[2] == 0
        assert store.manifests()[0]["state"] == "failed"


def test_forming_candle_excluded_even_if_provider_returns_it(tmp_path: Path) -> None:
    class IncludesCurrent(FakeClient):
        def klines(
            self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
        ) -> list[list[object]]:
            return [row_at(0), row_at(1), row_at(2)]

    end = LAUNCH + timedelta(minutes=2)
    with FuturesSQLite(tmp_path / "test.db") as store:
        assert sync_range(
            store, IncludesCurrent(), DEFAULT_SYMBOL, LAUNCH, end, as_of=end + timedelta(seconds=20)
        ).complete
        assert store.bounds(DEFAULT_SYMBOL)[2] == 2
        assert all(c.close_time <= end for c in store.read(DEFAULT_SYMBOL, LAUNCH, end))
        with pytest.raises(ValueError):
            sync_range(
                store,
                FakeClient(),
                DEFAULT_SYMBOL,
                LAUNCH,
                end + timedelta(minutes=1),
                as_of=end + timedelta(seconds=20),
            )


@pytest.mark.parametrize("limit", [True, 0, 1501, -1])
def test_limit_validation(tmp_path: Path, limit: int) -> None:
    with FuturesSQLite(tmp_path / "test.db") as store:
        with pytest.raises(ValueError):
            sync_range(
                store,
                FakeClient(),
                DEFAULT_SYMBOL,
                LAUNCH,
                LAUNCH + timedelta(minutes=1),
                as_of=AS_OF,
                limit=limit,
            )
