"""Medidas reproducibles de un fixture financiero acotado, sin datos reales."""

import json
import resource
from decimal import Decimal
from time import perf_counter

import pytest

from trading_engine.replay.encoding import encoded
from trading_engine.replay.historical_validation.benchmark import benchmark_audit

from .test_reserve_transfer_replay import ReserveRound
from .test_reserve_transfer_replay import reserve_round as reserve_round
from .test_reserve_transfer_replay import risk_round as risk_round


def test_synthetic_one_day_streaming_memory_bound() -> None:
    result = benchmark_audit(1)
    assert result.rows == 1440 and result.journal_records == 0
    assert result.peak_python_bytes < 4 * 1024 * 1024
    assert result.mode == "SYNTHETIC_STREAM_NOT_HISTORICAL_REPLAY"


def test_bounded_replay_snapshot_recovery_metrics(request: pytest.FixtureRequest) -> None:
    measured_round = request.getfixturevalue("reserve_round")
    assert isinstance(measured_round, ReserveRound)
    # Reutiliza la ronda sintética aprobada, con Base/DCA7/TP y un ADD modelado.
    started = perf_counter()
    runner = measured_round.runner()
    report = runner.run()
    run_seconds = Decimal(str(perf_counter() - started))
    started = perf_counter()
    snapshot = runner.snapshot
    integrity = snapshot.calculate_integrity()
    snapshot_seconds = Decimal(str(perf_counter() - started))
    assert integrity == snapshot.integrity_digest
    started = perf_counter()
    resumed = measured_round.runner(snapshot)
    recovery_seconds = Decimal(str(perf_counter() - started))
    assert resumed.snapshot == snapshot
    assert resumed.run() == report == measured_round.expected
    assert snapshot.performance is not None and snapshot.risk is not None
    assert snapshot.reserve is not None and snapshot.ledger is not None
    encoded_bytes = len(encoded(report))
    measurements = {
        "synthetic_evaluated_minutes": report.stats.evaluated_read,
        "synthetic_total_minutes": report.stats.minutes_read,
        "run_seconds": str(run_seconds),
        "snapshot_hash_seconds": str(snapshot_seconds),
        "restore_validation_seconds": str(recovery_seconds),
        "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "performance_journal": len(snapshot.performance.journal),
        "equity_series": len(snapshot.performance.series),
        "risk_journal": len(snapshot.risk.journal),
        "risk_evaluations": len(snapshot.risk.evaluations),
        "reserve_journal": len(snapshot.reserve.journal),
        "reserve_samples": len(snapshot.reserve.samples),
        "ledger_journal": len(snapshot.ledger.journal),
        "pending_transfers": len(snapshot.reserve.queue),
        "report_encoded_bytes": encoded_bytes,
        "fills": len(snapshot.ledger.entries),
    }
    print("SYNTHETIC_RECOVERY_BENCHMARK=" + json.dumps(measurements, sort_keys=True))
