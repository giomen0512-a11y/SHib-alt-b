"""Benchmarks acotados y exclusivamente sintéticos del registro, sin Replay financiero."""

import csv
import resource
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import perf_counter

from trading_engine.binance.klines import LAUNCH, to_ms

from .contracts import Contract, DatasetDeclaration, DatasetType, units
from .query import HistoricalQuery
from .reconciliation import HistoricalReconciliation
from .registry import HistoricalDatasetRegistry
from .segments import Registration, RegistryProvenance, SegmentBounds


@dataclass(frozen=True)
class RegistryBenchmark:
    days: int
    records: int
    registration_seconds: float
    reconciliation_seconds: float
    records_per_second: float
    peak_rss_kib: int
    sqlite_bytes: int
    query_seconds: float
    recovery_seconds: float
    prefix_query_seconds: float
    recognized_prefix_records: int
    dataset: str = "SYNTHETIC_FIXTURE_NOT_BINANCE_DATA"


def benchmark_registry(days: int, directory: Path) -> RegistryBenchmark:
    if type(days) is not int or days not in {1, 7, 30}:
        raise ValueError("Benchmark sintético limitado a 1/7/30 días")
    directory.mkdir(parents=True, exist_ok=True)
    original = directory / "SYNTHETIC_FIXTURE.csv"
    database = directory / "registry.sqlite3"
    if original.exists() or database.exists():
        raise ValueError("Benchmark exige destinos nuevos; nunca sobrescribe datos")
    end = LAUNCH + timedelta(days=days)
    count = days * 1440
    with original.open("x", newline="") as stream:
        writer = csv.writer(stream)
        for index in range(count):
            opened = to_ms(LAUNCH + timedelta(minutes=index))
            writer.writerow(
                (
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
            )
    declaration = DatasetDeclaration(
        "SYNTHETIC_FIXTURE",
        DatasetType.TRADE,
        Contract(),
        "1m",
        LAUNCH,
        end,
        "local:synthetic-only",
        "SYNTHETIC_FIXTURE_NOT_OFFICIAL",
        end + timedelta(days=1),
        "historical-local-v1",
        units(DatasetType.TRADE),
    )
    spec = Registration(
        declaration,
        str(original),
        "csv",
        end + timedelta(days=2),
        RegistryProvenance.FIXTURE,
        "Local deterministic synthetic generator",
        tuple(
            SegmentBounds(LAUNCH + timedelta(days=i), LAUNCH + timedelta(days=i + 1))
            for i in range(days)
        ),
    )
    registry = HistoricalDatasetRegistry(database)
    start = perf_counter()
    registered = registry.register(spec)
    registration_seconds = perf_counter() - start
    query = HistoricalQuery(database, (registered.dataset_id,))
    start = perf_counter()
    report = HistoricalReconciliation(query).evaluate(DatasetType.TRADE, LAUNCH, end, cutoff=end)
    reconciliation_seconds = perf_counter() - start
    if report.recognized != count or report.coverage_status != "AVAILABLE_FOR_REVIEW":
        raise ValueError("Benchmark no validó la cobertura del fixture")
    start = perf_counter()
    read_count = sum(1 for _ in query.read_trade(LAUNCH, end, cutoff=end))
    query_seconds = perf_counter() - start
    if read_count != count:
        raise ValueError("Consulta del benchmark perdió registros")
    start = perf_counter()
    recovered = HistoricalDatasetRegistry(database).verify(registered.dataset_id)
    recovery_seconds = perf_counter() - start
    if recovered != registered:
        raise ValueError("Recovery del benchmark divergente")
    start = perf_counter()
    prefix_count = sum(
        1 for _ in query.read_trade(LAUNCH, end, cutoff=LAUNCH + timedelta(minutes=3))
    )
    prefix_query_seconds = perf_counter() - start
    if prefix_count != 3:
        raise ValueError("Benchmark de prefijo causal divergente")
    return RegistryBenchmark(
        days,
        count,
        registration_seconds,
        reconciliation_seconds,
        count / registration_seconds,
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        database.stat().st_size,
        query_seconds,
        recovery_seconds,
        prefix_query_seconds,
        prefix_count,
    )
