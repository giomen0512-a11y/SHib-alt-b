"""Escala sintética acotada y barrera de no regresión de los motores aprobados."""

import csv
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_engine.binance.klines import to_ms
from trading_engine.replay.historical_validation.contracts import DatasetType
from trading_engine.replay.historical_validation.query import HistoricalQuery
from trading_engine.replay.historical_validation.reconciliation import HistoricalReconciliation
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.registry_benchmark import benchmark_registry
from trading_engine.replay.historical_validation.segments import SegmentBounds

from .test_historical_registry import spec


def test_registry_benchmark_streaming_and_recovery(tmp_path: Path) -> None:
    result = benchmark_registry(1, tmp_path)
    assert result.records == 1440
    assert result.recognized_prefix_records == 3
    assert result.sqlite_bytes > 0
    assert result.registration_seconds > 0
    assert result.recovery_seconds > 0
    assert result.peak_rss_kib > 0
    with pytest.raises(ValueError, match="destinos nuevos"):
        benchmark_registry(1, tmp_path)


@pytest.mark.parametrize("days", [0, 2, 31, True])
def test_registry_benchmark_rejects_unbounded_work(tmp_path: Path, days: int) -> None:
    with pytest.raises(ValueError):
        benchmark_registry(days, tmp_path)


def test_actual_synthetic_month_gap_and_complement(tmp_path: Path) -> None:
    start = datetime(2024, 4, 1, tzinfo=UTC)
    end = datetime(2024, 5, 1, tzinfo=UTC)
    base = spec(tmp_path, "monthly", [], start=start, end=end, file_format="csv")
    base = replace(
        base,
        declaration=replace(base.declaration, acquisition_time=end + timedelta(days=1)),
        validation_time=end + timedelta(days=2),
    )
    day_start = start + timedelta(days=10)
    day_end = day_start + timedelta(days=1)
    daily = replace(
        base,
        original_file=str(tmp_path / "daily.csv"),
        declaration=replace(
            base.declaration,
            dataset_id="daily",
            source_url_declared="local:daily",
            period_start=day_start,
            period_end=day_end,
        ),
        segments=(SegmentBounds(day_start, day_end),),
    )
    with (
        Path(base.original_file).open("w", newline="") as month_stream,
        Path(daily.original_file).open("w", newline="") as day_stream,
    ):
        month_writer, day_writer = csv.writer(month_stream), csv.writer(day_stream)
        for minute in range(43200):
            at = start + timedelta(minutes=minute)
            opened = to_ms(at)
            row = (
                opened,
                "100",
                "103",
                "99",
                "101",
                "10",
                opened + 59999,
                "1000",
                2,
                "5",
                "500",
                "0",
            )
            (day_writer if day_start <= at < day_end else month_writer).writerow(row)
    registry = HistoricalDatasetRegistry(tmp_path / "registry.sqlite3")
    monthly_id = registry.register(base).dataset_id
    a = HistoricalReconciliation(HistoricalQuery(registry.path, (monthly_id,)))
    partial = a.evaluate(DatasetType.TRADE, start, end, cutoff=end)
    assert partial.recognized == 41760
    assert partial.coverage_status == "PARTIAL"
    assert len(partial.gaps) == 1
    daily_id = registry.register(daily).dataset_id
    complete = HistoricalReconciliation(HistoricalQuery(registry.path, (monthly_id, daily_id)))
    report = complete.evaluate(DatasetType.TRADE, start, end, cutoff=end)
    assert report.recognized == 43200
    assert report.coverage_status == "AVAILABLE_FOR_REVIEW"
    matrix = complete.coverage_matrix(start, end, cutoff=end)
    trade = next(m for m in matrix if m.coverage.dataset_type is DatasetType.TRADE)
    assert trade.coverage.expected_records == 43200
    assert trade.coverage.coverage_percent == 100
