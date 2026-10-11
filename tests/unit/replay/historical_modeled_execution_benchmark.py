"""5D2: medir preparación, Quotes y Replay sintéticos por separado, sin red."""

import argparse
import csv
import json
import resource
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from trading_engine.binance.klines import CSV_HEADER, DEFAULT_SYMBOL, to_ms
from trading_engine.models.market import Candle
from trading_engine.replay.encoding import encoded
from trading_engine.replay.execution_model.quotes import ModeledQuotes
from trading_engine.replay.historical_validation.contracts import (
    Contract,
    DatasetDeclaration,
    DatasetType,
    units,
)
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.replay_adapter import (
    PreparedBlock,
    PreparedTradeHistory,
)
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.segments import (
    Registration,
    RegistryProvenance,
    SegmentBounds,
)
from trading_engine.replay.historical_validation.verification import SessionPolicy, VerificationMode
from trading_engine.replay.models import ReplayRequest
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution.core import SimulatedExecution

from .test_simulated_execution import simulation_policy


def rows(first: datetime, count: int) -> Iterator[tuple[object, ...]]:
    for i in range(count):
        opened = to_ms(first + timedelta(minutes=i))
        # Sin señales; volumen/taker explícitos, ninguna referencia financiera.
        yield (opened, "100", "103", "99", "100", "10", opened + 59999, "1000", 2, "5", "500", "0")


def benchmark(minutes: int, mode: VerificationMode) -> dict[str, object]:
    start = datetime(2026, 1, 2, tzinfo=UTC)
    first, end = start - timedelta(minutes=1560), start + timedelta(minutes=minutes)
    request = ReplayRequest(start, end)
    with tempfile.TemporaryDirectory(prefix="shib-adapter-benchmark-") as folder:
        source = Path(folder) / "synthetic.csv"
        with source.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(CSV_HEADER)
            writer.writerows(rows(first, 1560 + minutes))
        bounds: list[SegmentBounds] = []
        left = first
        while left < end:
            right = min(left + timedelta(days=1), end)
            bounds.append(SegmentBounds(left, right))
            left = right
        declaration = DatasetDeclaration(
            "synthetic-benchmark",
            DatasetType.TRADE,
            Contract(),
            "1m",
            first,
            end,
            "local:SYNTHETIC_FIXTURE",
            "No Binance source",
            end + timedelta(days=1),
            "historical-local-v1",
            units(DatasetType.TRADE),
        )
        registration = Registration(
            declaration,
            str(source),
            "csv",
            end + timedelta(days=2),
            RegistryProvenance.FIXTURE,
            "Local deterministic generated fixture",
            tuple(bounds),
        )
        path = Path(folder) / "registry.sqlite3"
        before = perf_counter()
        identity = HistoricalDatasetRegistry(path).register(registration).dataset_id
        registration_seconds = perf_counter() - before
        active = None
        before = perf_counter()
        if mode is VerificationMode.SEALED_SEGMENT_SESSION:
            active = SealedSegmentSession(
                path,
                (identity,),
                session_identity="benchmark-session",
                opened_at=end,
                policy=SessionPolicy(
                    3600, 100, "Synthetic source remains immutable in this process"
                ),
            )
        session_seconds = perf_counter() - before
        try:
            with PreparedTradeHistory(
                path, (identity,), request, mode=mode, session=active
            ) as history:
                policy = simulation_policy()
                quotes = ModeledQuotes(history, policy.pricing, history.evidence.identity)
                block_loads = 0
                block = history._block

                def counted(root: PreparedBlock) -> dict[datetime, Candle]:
                    nonlocal block_loads
                    if history._cached_block != root:
                        block_loads += 1
                    return block(root)

                with patch.object(history, "_block", side_effect=counted):
                    before = perf_counter()
                    quotes_available = 0
                    for i in range(minutes):
                        at = start + timedelta(
                            minutes=i, milliseconds=policy.pricing.quote_latency_ms
                        )
                        if quotes.at(DEFAULT_SYMBOL, at) is not None:
                            quotes_available += 1
                    quote_seconds = perf_counter() - before
                quote_block_loads = block_loads
                block_loads = 0
                before = perf_counter()
                runner = ReplayRunner(
                    history, request, quotes=quotes, simulator=SimulatedExecution(policy, quotes)
                )
                with patch.object(history, "_block", side_effect=counted):
                    expected = runner.run()
                replay_seconds = perf_counter() - before
                replay_block_loads = block_loads
                fragmented = ReplayRunner(
                    history, request, quotes=quotes, simulator=SimulatedExecution(policy, quotes)
                )
                fragmented.run(start + timedelta(minutes=minutes // 2))
                before = perf_counter()
                resumed = ReplayRunner(
                    history,
                    request,
                    quotes=quotes,
                    simulator=SimulatedExecution(policy, quotes),
                    snapshot=fragmented.snapshot,
                )
                restore_seconds = perf_counter() - before
                before = perf_counter()
                actual = resumed.run()
                remaining_seconds = perf_counter() - before
                if actual != expected or resumed.snapshot != runner.snapshot:
                    raise AssertionError("Continuous/fragmented replay diverged")
                result: dict[str, object] = {
                    "provenance": "SYNTHETIC_FIXTURE",
                    "mode": mode.value,
                    "evaluated_minutes": minutes,
                    "warmup_minutes": 1560,
                    "registration_seconds": registration_seconds,
                    "initial_session_validation_seconds": session_seconds,
                    "preparation_including_validation_seconds": history.metrics[
                        "preparation_seconds"
                    ],
                    "reconciliation_validation_seconds": history.metrics["validation_seconds"],
                    "replay_seconds": replay_seconds,
                    "quotes_seconds": quote_seconds,
                    "quote_queries": minutes,
                    "quotes_available": quotes_available,
                    "quote_verified_block_loads": quote_block_loads,
                    "replay_verified_block_loads": replay_block_loads,
                    "quote_dataset_identity": quotes.dataset_identity,
                    "restore_seconds": restore_seconds,
                    "remaining_replay_seconds": remaining_seconds,
                    "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    * 1024,
                    "prepared_sqlite_bytes": history.metrics["temporary_bytes"],
                    "session_sqlite_bytes": 0
                    if active is None
                    else active.cache_path.stat().st_size,
                    "bounded_candle_block_rows": 1440,
                    "prepared_rows": history.metrics["rows"],
                    "event_digest": actual.event_digest,
                    "report_sha256": sha256(encoded(actual)).hexdigest(),
                    "snapshot_integrity": resumed.snapshot.integrity_digest,
                    "continuous_fragmented_equal": True,
                    "economic_computation": (
                        "MODELED_QUOTES_AND_EXECUTION_NO_ENTRIES_NO_FINANCIAL_MODELS"
                    ),
                    "account_wallet": "NOT_AVAILABLE",
                }
        finally:
            if active is not None:
                active.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, choices=(20, 1440, 10080), required=True)
    parser.add_argument("--mode", choices=[v.value for v in VerificationMode], required=True)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.minutes, VerificationMode(args.mode)), sort_keys=True))
