"""Medición sintética por proceso: preparación streaming y publicación en páginas."""

import argparse
import csv
import json
import resource
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from trading_engine.binance.klines import CSV_HEADER, to_ms
from trading_engine.replay.encoding import encoded
from trading_engine.replay.historical_validation.contracts import (
    Contract,
    DatasetDeclaration,
    DatasetType,
    units,
)
from trading_engine.replay.historical_validation.mark_history import PreparedMarkHistory
from trading_engine.replay.historical_validation.mark_source import (
    HistoricalMarkPriceSource,
    MarkPublicationPolicy,
)
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.segments import (
    Registration,
    RegistryProvenance,
    SegmentBounds,
)
from trading_engine.replay.historical_validation.verification import SessionPolicy, VerificationMode

MINUTE = timedelta(minutes=1)


def benchmark(minutes: int, mode: VerificationMode) -> dict[str, object]:
    start = datetime(2026, 1, 2, tzinfo=UTC)
    end = start + minutes * MINUTE
    with tempfile.TemporaryDirectory(prefix="mark-benchmark-") as folder, ExitStack() as stack:
        directory = Path(folder)
        original = directory / "mark.csv"
        with original.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(CSV_HEADER)
            for i in range(minutes):
                opened = to_ms(start + i * MINUTE)
                writer.writerow(
                    (opened, "95", "95", "95", "95", "0", opened + 59999, "0", 0, "0", "0", "0")
                )
        segments = []
        left = start
        while left < end:
            right = min(left + timedelta(days=1), end)
            segments.append(SegmentBounds(left, right))
            left = right
        declaration = DatasetDeclaration(
            "synthetic-mark-benchmark",
            DatasetType.MARK,
            Contract(),
            "1m",
            start,
            end,
            "local:SYNTHETIC_FIXTURE",
            "Synthetic Mark, no Binance observation",
            end + timedelta(days=1),
            "historical-local-v1",
            units(DatasetType.MARK),
        )
        registration = Registration(
            declaration,
            str(original),
            "csv",
            end + timedelta(days=2),
            RegistryProvenance.FIXTURE,
            "Synthetic generated close samples",
            tuple(segments),
        )
        path = directory / "registry.sqlite3"
        before = perf_counter()
        ids = (HistoricalDatasetRegistry(path).register(registration).dataset_id,)
        register_seconds = perf_counter() - before
        before = perf_counter()
        session = None
        if mode is VerificationMode.SEALED_SEGMENT_SESSION:
            session = stack.enter_context(
                SealedSegmentSession(
                    path,
                    ids,
                    session_identity="synthetic-mark-session",
                    opened_at=end,
                    policy=SessionPolicy(
                        3600, 10000, "Immutable synthetic original in controlled process"
                    ),
                )
            )
        session_seconds = perf_counter() - before
        history = stack.enter_context(
            PreparedMarkHistory(path, ids, start, end, cutoff=end, mode=mode, session=session)
        )
        before = perf_counter()
        count = sum(1 for _ in history.read(0, end))
        read_seconds = perf_counter() - before
        policy = MarkPublicationPolicy(
            0,
            59999,
            10080,
            "SYNTHETIC_FIXTURE_ONLY",
            "Synthetic sampled Mark; latency assumed zero",
        )
        source = HistoricalMarkPriceSource(history, policy)

        def convert(first_page: int) -> tuple[int, str]:
            cursor = 0
            fingerprint = sha256()
            while cursor < minutes:
                size = first_page if cursor == 0 else 10080
                until = min(end, start + (cursor + size) * MINUTE)
                page_count = 0
                for event in source.read(cursor, until):
                    assert event.sequence == cursor
                    fingerprint.update(
                        encoded(
                            (
                                event.sequence,
                                event.payload.event_id,
                                event.payload.price,
                                event.payload.effective_at,
                                event.received_at,
                            )
                        )
                    )
                    cursor += 1
                    page_count += 1
                assert page_count
            return cursor, fingerprint.hexdigest()

        before = perf_counter()
        full = convert(10080)
        conversion_seconds = perf_counter() - before
        before = perf_counter()
        resumed = convert(3)
        resume_seconds = perf_counter() - before
        assert full == resumed and count == minutes
        blocked = False
        if minutes > 10080:
            try:
                next(source.read(0, end))
            except ValueError as exc:
                assert str(exc) == "MARK_PUBLICATION_LIMIT_EXCEEDED"
                blocked = True
            assert blocked
        return dict(
            provenance="SYNTHETIC_FIXTURE",
            mode=mode.value,
            minutes=minutes,
            registration_seconds=register_seconds,
            session_initial_seconds=session_seconds,
            validation_seconds=history.metrics["validation_seconds"],
            preparation_seconds=history.metrics["preparation_seconds"],
            read_seconds=read_seconds,
            conversion_seconds=conversion_seconds,
            resume_seconds=resume_seconds,
            peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            prepared_sqlite_bytes=history.path.stat().st_size,
            registry_sqlite_bytes=path.stat().st_size,
            session_sqlite_bytes=0 if session is None else session.cache_path.stat().st_size,
            events=count,
            blocks=len(history.blocks),
            max_cached_rows=len(history._rows),
            continuous_fragmented_equal=True,
            economic_digest=full[1],
            large_single_read_blocked=blocked,
            financial_runner_multi_year="BLOCKED_REQUIRES_SEPARATE_CURSOR_AND_JOURNAL_DESIGN",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, choices=(20, 1440, 10080, 43200), required=True)
    parser.add_argument("--mode", choices=[m.value for m in VerificationMode], required=True)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.minutes, VerificationMode(args.mode)), sort_keys=True))
