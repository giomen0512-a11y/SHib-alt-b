"""Medición acotada de auditoría sintética; nunca un backtest histórico."""

import resource
import tracemalloc
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext
from time import perf_counter

from trading_engine.binance.klines import LAUNCH, to_ms
from trading_engine.replay.financial_ledger.policy import ledger_context

from .audit import audit_rows
from .contracts import Contract, DatasetDeclaration, DatasetType, units


@dataclass(frozen=True)
class AuditBenchmark:
    synthetic_days: int
    rows: int
    seconds: Decimal
    rows_per_second: Decimal
    peak_python_bytes: int
    process_peak_rss_kib: int
    journal_records: int
    mode: str = "SYNTHETIC_STREAM_NOT_HISTORICAL_REPLAY"


def benchmark_audit(days: int) -> AuditBenchmark:
    if type(days) is not int or days not in {1, 7, 30}:
        raise ValueError("Benchmark acotado a 1, 7 o 30 días sintéticos")
    if tracemalloc.is_tracing():
        raise ValueError("Benchmark requiere tracemalloc independiente")
    count = days * 1440
    end = LAUNCH + timedelta(days=days)
    declaration = DatasetDeclaration(
        "SYNTHETIC_BENCHMARK",
        DatasetType.TRADE,
        Contract(),
        "1m",
        LAUNCH,
        end,
        "local:SYNTHETIC_FIXTURE",
        "NO_EXTERNAL_DATA",
        end,
        "historical-local-v1",
        units(DatasetType.TRADE),
    )

    def rows() -> Iterator[list[object]]:
        opened = to_ms(LAUNCH)
        for i in range(count):
            at = opened + i * 60_000
            yield [at, "100", "101", "99", "100", "10", at + 59999, "1000", 1, "5", "500", "0"]

    tracemalloc.start()
    try:
        started = perf_counter()
        result, _ = audit_rows(rows(), declaration, as_of=end)
        elapsed = Decimal(str(perf_counter() - started))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if result.rows != count or not result.coverage_complete:
        raise ValueError("Benchmark no validó el fixture completo")
    with localcontext(ledger_context()):
        throughput = Decimal(count) / elapsed
    return AuditBenchmark(
        days,
        count,
        elapsed,
        throughput,
        peak,
        int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        0,
    )
