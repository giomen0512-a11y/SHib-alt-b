from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading_engine.utils.time import require_utc


def test_accepts_utc() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    assert require_utc(timestamp) == timestamp
    assert require_utc(timestamp).tzinfo is UTC


def test_rejects_naive() -> None:
    with pytest.raises(ValueError, match="UTC timezone-aware"):
        require_utc(datetime(2026, 1, 1))


def test_rejects_non_utc() -> None:
    with pytest.raises(ValueError, match="UTC timezone-aware"):
        require_utc(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-6))))
