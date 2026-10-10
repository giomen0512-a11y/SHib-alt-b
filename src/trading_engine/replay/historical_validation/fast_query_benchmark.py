"""Mediciones sintéticas comparables; floats sólo para tiempos/memoria diagnósticos."""

import csv
import resource
import tracemalloc
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from trading_engine.binance.klines import LAUNCH, to_ms
from trading_engine.replay.encoding import encoded

from .contracts import Contract, DatasetDeclaration, DatasetType, units
from .fast_query import QueryStream, SegmentedHistoricalQuery
from .query import HistoricalEvent, HistoricalQuery
from .reconciliation import HistoricalReconciliation
from .registry import HistoricalDatasetRegistry
from .segment_session import SealedSegmentSession
from .segments import Registration, RegistryProvenance, SegmentBounds
from .verification import SessionPolicy, checkpoint_from_json


@dataclass(frozen=True)
class QueryMeasurement:
    case: str
    mode: str
    temperature: str
    verification: str
    seconds: float
    recognized: int
    causal_digest: str
    peak_rss_kib: int
    python_peak_bytes: int
    estimated_source_bytes: int
    sqlite_bytes: int
    sidecar_bytes: int
    query_spool_bytes: int = 0
    memory_scope: str = "TRACEMALLOC_OPERATION;RSS_PROCESS_HIGH_WATER"


def synthetic_fixture(days: int, directory: Path) -> tuple[Path, Registration]:
    if type(days) is not int or days not in {1, 7, 30}:
        raise ValueError("Sólo fixtures sintéticos 1/7/30 días")
    directory.mkdir(parents=True, exist_ok=True)
    original, database = directory / "SYNTHETIC.csv", directory / "registry.sqlite3"
    if original.exists() or database.exists():
        raise ValueError("Destinos nuevos requeridos")
    end = LAUNCH + timedelta(days=days)
    with original.open("x", newline="") as out:
        writer = csv.writer(out)
        for i in range(days * 1440):
            at = to_ms(LAUNCH + timedelta(minutes=i))
            writer.writerow(
                (at, "100", "103", "99", "101", "10", at + 59999, "1000", 2, "5", "500", "0")
            )
    declaration = DatasetDeclaration(
        "SYNTHETIC",
        DatasetType.TRADE,
        Contract(),
        "1m",
        LAUNCH,
        end,
        "local:synthetic",
        "NOT_BINANCE_DATA",
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
        "Deterministic local fixture",
        tuple(
            SegmentBounds(LAUNCH + timedelta(days=i), LAUNCH + timedelta(days=i + 1))
            for i in range(days)
        ),
    )
    return database, spec


def consume(events: Iterator[HistoricalEvent]) -> tuple[int, str]:
    fingerprint, count = sha256(), 0
    for event in events:
        count += 1
        fingerprint.update(encoded(event.economic_key()))
    if isinstance(events, QueryStream) and events.state != "COMPLETE":
        raise ValueError("Benchmark stream failed finalization")
    return count, fingerprint.hexdigest()


def benchmark_fast_query(
    days: int,
    directory: Path,
    *,
    on_measurement: Callable[[QueryMeasurement], None] | None = None,
    process_restart: Callable[[Path, Path, str], QueryMeasurement] | None = None,
) -> tuple[QueryMeasurement, ...]:
    """Cold = raíz nueva (no cache de sesión); no significa desalojar la cache del SO."""
    database, spec = synthetic_fixture(days, directory)
    source_bytes = Path(spec.original_file).stat().st_size
    results: list[QueryMeasurement] = []
    registry = HistoricalDatasetRegistry(database)
    ids: list[str] = []
    active: list[SealedSegmentSession] = []
    spool_bytes = 0

    def measured_consume(events: Iterator[HistoricalEvent]) -> tuple[int, str]:
        nonlocal spool_bytes
        if isinstance(events, QueryStream):
            spool_bytes = max(spool_bytes, events.path.stat().st_size)
        return consume(events)

    def measure(
        case: str,
        mode: str,
        temperature: str,
        verification: str,
        action: Callable[[], tuple[int, str]],
        reads: int,
        *,
        storage_path: Path | None = None,
        sidecar_path: Path | None = None,
    ) -> QueryMeasurement:
        nonlocal spool_bytes
        spool_bytes = 0
        if tracemalloc.is_tracing():
            raise ValueError("Benchmark requires exclusive tracemalloc ownership")
        tracemalloc.start()
        started = perf_counter()
        try:
            count, fingerprint = action()
            seconds = perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        sidecar = (
            active[0].cache_path.stat().st_size if active and active[0].cache_path.exists() else 0
        )
        if sidecar_path is not None:
            sidecar = sidecar_path.stat().st_size
        result = QueryMeasurement(
            case,
            mode,
            temperature,
            verification,
            seconds,
            count,
            fingerprint,
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            peak,
            reads,
            (storage_path or database).stat().st_size,
            sidecar,
            spool_bytes,
        )
        results.append(result)
        if on_measurement is not None:
            on_measurement(result)
        return result

    def register() -> tuple[int, str]:
        data = registry.register(spec)
        ids.append(data.dataset_id)
        return data.row_count, data.source_hash

    measure(
        "A_REGISTER",
        "STRICT_FULL",
        "COLD_START",
        "FULL_SOURCE_VERIFIED",
        register,
        source_bytes * 9,
    )
    measure(
        "B_FIRST_FULL_VALIDATION",
        "STRICT_FULL",
        "FULL_VALIDATION",
        "FULL_SOURCE_VERIFIED",
        lambda: (registry.verify(ids[0]).row_count, ids[0]),
        source_bytes * 6,
    )

    def open_session() -> tuple[int, str]:
        active.append(
            SealedSegmentSession(
                database,
                tuple(ids),
                session_identity="benchmark-root",
                opened_at=spec.validation_time,
                policy=SessionPolicy(7200, 1000, "Controlled synthetic fixture immutable"),
            )
        )
        return days * 1440, active[0].root_id

    opening = measure(
        "D_OPEN_VALIDATED_SESSION",
        "SEALED_SEGMENT_SESSION",
        "COLD_START",
        "FULL_SOURCE_VERIFIED",
        open_session,
        source_bytes * 9,
    )
    results.append(
        QueryMeasurement(
            "C_BUILD_PHYSICAL_INDEX",
            opening.mode,
            "PREPARATION_COMPONENT",
            "SESSION_VERIFIED",
            active[0].metrics["index_seconds"],
            days * 1440,
            active[0].root_id,
            opening.peak_rss_kib,
            opening.python_peak_bytes,
            source_bytes * 3,
            opening.sqlite_bytes,
            opening.sidecar_bytes,
            0,
            "UPPER_BOUND_SHARED_SESSION_OPEN_PEAK",
        )
    )
    strict, fast = HistoricalQuery(database, tuple(ids)), SegmentedHistoricalQuery(active[0])
    expected: dict[str, tuple[int, str]] = {}
    try:
        windows = [("E_PREFIX_3M", timedelta(minutes=3)), ("F_DAY", timedelta(days=1))]
        if days >= 7:
            windows.append(("G_7D", timedelta(days=7)))
        if days >= 30:
            windows.append(("H_30D", timedelta(days=30)))
        for label, interval in windows:
            end = LAUNCH + interval

            def strict_action(end: datetime = end) -> tuple[int, str]:
                return measured_consume(strict.read_trade(LAUNCH, end, cutoff=end))

            def fast_action(end: datetime = end) -> tuple[int, str]:
                return measured_consume(fast.read_trade(LAUNCH, end, cutoff=end))

            baseline = measure(
                label,
                "STRICT_FULL",
                "FULL_VALIDATION",
                "FULL_SOURCE_VERIFIED",
                strict_action,
                source_bytes * 14
                + 2
                * source_bytes
                * (interval.days * 1440 + interval.seconds // 60)
                // (days * 1440),
            )
            expected[label] = (baseline.recognized, baseline.causal_digest)
            optimized = measure(
                label,
                "SEALED_SEGMENT_SESSION",
                "WARM_SESSION",
                "SEGMENT_VERIFIED",
                fast_action,
                source_bytes * max(1, interval.days) // days,
            )
            if (optimized.recognized, optimized.causal_digest) != expected[label]:
                raise ValueError("STRICT/segment benchmark divergence")
        stream = fast.read(
            DatasetType.TRADE,
            LAUNCH,
            LAUNCH + timedelta(minutes=3),
            cutoff=spec.declaration.period_end,
        )
        first = next(stream)
        checkpoint = stream.checkpoint()
        stream.close()

        def recover() -> tuple[int, str]:
            resumed = fast.resume(checkpoint)
            rest = measured_consume(resumed)
            if rest[0] != 2:
                raise ValueError("Recovery duplicated/omitted events")
            return 1 + rest[0], resumed.causal_digest

        recovery = measure(
            "I_INTERRUPTED_RECOVERY",
            "SEALED_SEGMENT_SESSION",
            "WARM_SESSION",
            "SEGMENT_VERIFIED",
            recover,
            source_bytes // days,
        )
        if (recovery.recognized, recovery.causal_digest) != expected[
            "E_PREFIX_3M"
        ] or not first.event_id:
            raise ValueError("Recovery fingerprint mismatch")
        active[0].close()

        def reopen() -> tuple[int, str]:
            with SealedSegmentSession(
                database,
                tuple(ids),
                session_identity="fresh-root",
                opened_at=spec.validation_time,
                policy=SessionPolicy(7200, 1000, "New root with full revalidation"),
            ) as fresh:
                resumed = SegmentedHistoricalQuery(fresh).resume(
                    checkpoint, trusted_checkpoint_digest=checkpoint.checkpoint_digest
                )
                count, _ = consume(resumed)
                return count, resumed.causal_digest

        reopened = measure(
            "J_NEW_SESSION_RECOVERY",
            "SEALED_SEGMENT_SESSION",
            "COLD_START",
            "FULL_VALIDATION_THEN_SEGMENT_VERIFIED",
            reopen,
            source_bytes * 9 + source_bytes // days,
        )
        if reopened.recognized != 2 or reopened.causal_digest != recovery.causal_digest:
            raise ValueError("New-session recovery divergence")
        if process_restart is not None:
            # El paquete nunca lanza procesos. Harness externo de tests/medición opt-in.
            cp_file = directory / "query-checkpoint.json"
            cp_file.write_bytes(encoded(checkpoint))
            probe = process_restart(database, cp_file, checkpoint.checkpoint_digest)
            if probe.recognized != 2 or probe.causal_digest != recovery.causal_digest:
                raise ValueError("Process restart divergence")
            results.append(probe)

        measure(
            "K_INVALIDATION_REVERIFY",
            "STRICT_FULL",
            "FULL_VALIDATION",
            "FULL_SOURCE_VERIFIED",
            lambda: (registry.verify(ids[0]).row_count, ids[0]),
            source_bytes * 6,
        )
        # L/M usan tres minutos explícitos; no se disfrazan de un mes completo.
        for conflicted in (False, True):
            mini_dir = directory / ("conflict" if conflicted else "duplicate")
            mini_dir.mkdir()
            mini_file = mini_dir / "fixture.csv"
            with mini_file.open("x", newline="") as out:
                writer = csv.writer(out)
                for occurrence, index in enumerate((0, 1, 2, 2)):
                    at = to_ms(LAUNCH + timedelta(minutes=index))
                    writer.writerow(
                        (
                            at,
                            "100",
                            "103",
                            "99",
                            "102" if conflicted and occurrence == 3 else "101",
                            "10",
                            at + 59999,
                            "1000",
                            2,
                            "5",
                            "500",
                            "0",
                        )
                    )
            from dataclasses import replace

            mini_end = LAUNCH + timedelta(minutes=3)
            mini_spec = replace(
                spec,
                original_file=str(mini_file),
                declaration=replace(spec.declaration, period_end=mini_end),
                segments=(SegmentBounds(LAUNCH, mini_end),),
            )
            mini = HistoricalDatasetRegistry(mini_dir / "registry.sqlite3")
            data = mini.register(mini_spec)
            with SealedSegmentSession(
                mini.path,
                (data.dataset_id,),
                session_identity="mini",
                opened_at=spec.validation_time,
                policy=SessionPolicy(7200, 1000, "Synthetic mini fixture"),
            ) as mini_session:
                for query, mode in (
                    (HistoricalQuery(mini.path, (data.dataset_id,)), "STRICT_FULL"),
                    (SegmentedHistoricalQuery(mini_session), "SEALED_SEGMENT_SESSION"),
                ):

                    def reconcile(
                        query: HistoricalQuery = query,
                        mini_end: datetime = mini_end,
                        conflicted: bool = conflicted,
                    ) -> tuple[int, str]:
                        report = HistoricalReconciliation(query).evaluate(
                            DatasetType.TRADE, LAUNCH, mini_end, cutoff=mini_end
                        )
                        if bool(report.conflicts) != conflicted:
                            raise ValueError("Conflict diagnostic mismatch")
                        return report.recognized, report.causal_digest

                    measure(
                        "M_CONFLICT_3M" if conflicted else "L_DUPLICATE_3M",
                        mode,
                        "FULL_VALIDATION" if mode == "STRICT_FULL" else "WARM_SESSION",
                        "FULL_SOURCE_VERIFIED" if mode == "STRICT_FULL" else "SEGMENT_VERIFIED",
                        reconcile,
                        mini_file.stat().st_size * (8 if mode == "STRICT_FULL" else 1),
                        storage_path=mini.path,
                        sidecar_path=mini_session.cache_path,
                    )
        return tuple(results)
    finally:
        active[0].close()


def restart_probe(database: Path, checkpoint_file: Path, trusted_root: str) -> dict[str, object]:
    """Raíz recibida del caller, nunca tomada automáticamente del JSON no confiable."""
    cp = checkpoint_from_json(checkpoint_file.read_text())
    from .registry import decode_registration
    from .storage import connection

    with connection(database) as db:
        row = db.execute(
            "SELECT spec FROM datasets WHERE id=?", (cp.verification.dataset_ids[0],)
        ).fetchone()
        opened = decode_registration(row[0]).validation_time
    tracemalloc.start()
    started = perf_counter()
    try:
        with SealedSegmentSession(
            database,
            cp.verification.dataset_ids,
            session_identity="process-restart",
            opened_at=opened,
            policy=SessionPolicy(7200, 1000, "Explicit full revalidation after restart"),
        ) as fresh:
            stream = SegmentedHistoricalQuery(fresh).resume(
                cp, trusted_checkpoint_digest=trusted_root
            )
            count, _ = consume(stream)
            sidecar = fresh.cache_path.stat().st_size
            seconds = perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            return {
                "seconds": seconds,
                "recognized": count,
                "causal_digest": stream.causal_digest,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "python_peak_bytes": peak,
                "sidecar_bytes": sidecar,
            }
    finally:
        tracemalloc.stop()
