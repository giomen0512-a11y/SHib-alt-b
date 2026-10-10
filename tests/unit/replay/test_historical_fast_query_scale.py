"""1/7/30 días sintéticos; reanudación exacta y memoria streaming, sin Replay."""

from datetime import timedelta
from itertools import zip_longest
from pathlib import Path
from typing import Any

import pytest

from trading_engine.binance.klines import LAUNCH
from trading_engine.replay.historical_validation.contracts import DatasetType
from trading_engine.replay.historical_validation.fast_query import SegmentedHistoricalQuery
from trading_engine.replay.historical_validation.fast_query_benchmark import (
    benchmark_fast_query,
    consume,
    synthetic_fixture,
)
from trading_engine.replay.historical_validation.query import HistoricalQuery
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.verification import SessionPolicy

from .historical_query_process_probe import process_restart_measurement


@pytest.mark.parametrize("days", [1, 7, 30])
def test_scale_outputs_recovery_and_digest(tmp_path: Path, days: int) -> None:
    path, spec = synthetic_fixture(days, tmp_path)
    dataset = HistoricalDatasetRegistry(path).register(spec)
    ids = (dataset.dataset_id,)
    end = LAUNCH + timedelta(days=days)
    with SealedSegmentSession(
        path,
        ids,
        session_identity=f"scale-{days}",
        opened_at=spec.validation_time,
        policy=SessionPolicy(7200, 1000, "Synthetic immutable files"),
    ) as active:
        fast = SegmentedHistoricalQuery(active)
        full = fast.read(DatasetType.TRADE, LAUNCH, end, cutoff=end)
        strict = HistoricalQuery(path, ids).read_trade(LAUNCH, end, cutoff=end)
        count = 0
        for a, b in zip_longest(full, strict):
            assert a == b and a is not None
            count += 1
        assert count == days * 1440 and full.state == "COMPLETE"
        expected = full.causal_digest
        # Antes de primer registro, durante segmento, en frontera, antes de finalización.
        for position in (0, 3, min(1440, days * 1440 - 1), days * 1440):
            stream = fast.read(DatasetType.TRADE, LAUNCH, end, cutoff=end)
            for _ in range(position):
                next(stream)
            cp = stream.checkpoint()
            stream.close()
            recovered = fast.resume(cp)
            rest_count, _ = consume(recovered)
            assert rest_count + position == count
            assert recovered.causal_digest == expected and recovered.state == "COMPLETE"
        # Interrupción de groups, usado por conciliación, también tiene cursor exacto.
        groups = fast.groups(DatasetType.TRADE, LAUNCH, end, cutoff=end)
        next(groups)
        cp = groups.checkpoint()
        groups.close()
        recovered = fast.resume(cp)
        assert consume(recovered)[0] == count - 1 and recovered.causal_digest == expected
        # Sólo un segmento diario necesario para tres minutos; sin validar el mes otra vez.
        prefix = fast.read(DatasetType.TRADE, LAUNCH, LAUNCH + timedelta(minutes=3), cutoff=end)
        assert consume(prefix)[0] == 3 and len(prefix.verification.segment_ids) == 1


def test_benchmark_one_day_is_reproducible_and_complete(tmp_path: Path) -> None:
    results = benchmark_fast_query(1, tmp_path, process_restart=process_restart_measurement)
    names = {m.case for m in results}
    assert {
        "A_REGISTER",
        "B_FIRST_FULL_VALIDATION",
        "C_BUILD_PHYSICAL_INDEX",
        "D_OPEN_VALIDATED_SESSION",
        "E_PREFIX_3M",
        "F_DAY",
        "I_INTERRUPTED_RECOVERY",
        "J_PROCESS_RESTART",
        "K_INVALIDATION_REVERIFY",
        "L_DUPLICATE_3M",
        "M_CONFLICT_3M",
    } <= names
    prefix = [m for m in results if m.case == "E_PREFIX_3M"]
    assert len(prefix) == 2 and prefix[0].causal_digest == prefix[1].causal_digest
    assert all(m.seconds >= 0 and m.python_peak_bytes > 0 and m.peak_rss_kib > 0 for m in results)
    assert all(m.sqlite_bytes > 0 and m.estimated_source_bytes > 0 for m in results)
    assert all(m.recognized == 3 for m in prefix)
    with pytest.raises(ValueError, match="Destinos nuevos"):
        benchmark_fast_query(1, tmp_path)


@pytest.mark.parametrize("days", [0, 2, True, "30", -1])
def test_scale_generator_rejects_unapproved_sizes(tmp_path: Path, days: Any) -> None:
    with pytest.raises(ValueError):
        synthetic_fixture(days, tmp_path)
