"""AUD-5D3-001: invalidación real dentro de una sola llamada a run()."""

from datetime import timedelta
from pathlib import Path
from typing import Any, Never

import pytest

from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.models import (
    FinancialObservation,
    PerformanceState,
)
from trading_engine.replay.historical_validation import segment_session
from trading_engine.replay.historical_validation.mark_history import PreparedMarkHistory
from trading_engine.replay.historical_validation.mark_source import (
    HistoricalMarkEvent,
    HistoricalMarkPriceSource,
)
from trading_engine.replay.historical_validation.verification import VerificationMode
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import FixtureFacts, FixtureQuotes
from trading_engine.replay.runner import ReplayRunner

from .conftest import DUE, END, START
from .test_financial_performance import policy
from .test_historical_fast_query import registered
from .test_historical_mark_price import MINUTE, prepared, publication, registration

FAULTS = ("prepared_close", "session_close", "ttl", "source_changed")


def invalidate(
    history: PreparedMarkHistory, original: Path, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    history.check()
    if fault == "prepared_close":
        history.close()
        return
    active = history._source_session
    assert active is not None
    if fault == "session_close":
        active.close()
    elif fault == "ttl":
        monkeypatch.setattr(
            segment_session,
            "monotonic",
            lambda: active._started + active.policy.max_seconds + 1,
        )
    else:
        assert fault == "source_changed"
        with original.open("a") as stream:
            stream.write("\n")


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("boundary", ("before_tick", "after_ledger", "after_mark", "after_value"))
def test_mid_run_loss_preserves_recognized_financial_prefix(
    tmp_path: Path,
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    boundary: str,
) -> None:
    """No se sustituyen check/identity: se cierran o invalidan sus recursos reales.

    Los primeros tres puntos prueban la protección indirecta del HEAD auditado.
    El último prueba que tampoco se confirma un tick local al perder confianza.
    """
    reg = registration(tmp_path, start=START, count=int((END - START) // MINUTE))
    path, ids = registered(tmp_path, reg)
    mode = (
        VerificationMode.STRICT_FULL
        if fault == "prepared_close"
        else VerificationMode.SEALED_SEGMENT_SESSION
    )
    with prepared(path, ids, START, END, mode) as history:
        marks = HistoricalMarkPriceSource(history, publication())
        with SQLiteHistory(database) as trade:
            runner = ReplayRunner(
                trade,
                replay_request,
                quotes=quotes,
                facts=facts,
                ledger=FinancialLedger(),
                performance=FinancialPerformance(policy(capital=None), marks=marks),
            )
            loss_at = DUE + MINUTE
            prefix: ReplaySnapshot | None = None
            invalidated = False
            observe = FinancialPerformance.observe
            tick = runner._financial_tick
            record = runner._record
            financial_records_after_loss: list[object] = []
            observations_after_loss: list[FinancialObservation] = []

            def lose_trust() -> None:
                nonlocal invalidated
                assert not invalidated and prefix is not None
                assert runner.snapshot.clock.now == loss_at
                invalidate(history, Path(reg.original_file), fault, monkeypatch)
                invalidated = True

            def record_with_trust_boundary(kind: str, payload: object) -> None:
                if invalidated and kind == "performance_transition":
                    financial_records_after_loss.append(payload)
                record(kind, payload)

            def observe_then_invalidate(
                self: FinancialPerformance, state: PerformanceState, item: FinancialObservation
            ) -> PerformanceState:
                if invalidated:
                    observations_after_loss.append(item)
                result = observe(self, state, item)
                at_boundary = (
                    (boundary == "after_ledger" and isinstance(item.payload, LedgerState))
                    or (boundary == "after_mark" and isinstance(item.payload, HistoricalMarkEvent))
                    or (boundary == "after_value" and item.payload == "VALUE")
                )
                if item.at == loss_at and at_boundary and not invalidated:
                    lose_trust()
                return result

            def tick_with_invalidation() -> None:
                nonlocal prefix
                if runner.snapshot.clock.now == loss_at:
                    prefix = runner.snapshot
                    assert prefix.performance is not None
                    assert len(prefix.performance.events) == int((loss_at - START) // MINUTE) - 1
                    assert len(prefix.performance.series) >= 1
                    assert prefix.ledger is not None and prefix.ledger.entries
                    unrealized = prefix.performance.series[-1].unrealized.value
                    assert unrealized is not None and unrealized < 0
                    assert prefix.performance.series[-1].mark_id is not None
                    assert prefix.performance_pending is not None
                    assert isinstance(prefix.performance_pending[0][1], HistoricalMarkEvent)
                    assert prefix.performance_pending[0][1].received_at == loss_at
                    if boundary == "before_tick":
                        lose_trust()
                tick()

            monkeypatch.setattr(FinancialPerformance, "observe", observe_then_invalidate)
            monkeypatch.setattr(runner, "_financial_tick", tick_with_invalidation)
            monkeypatch.setattr(runner, "_record", record_with_trust_boundary)
            # Única llamada: no cerrar entre dos run() ni antes de iniciarlo.
            with pytest.raises(ValueError, match="NOT_AVAILABLE|SESSION_EXPIRED|SOURCE_CHANGED"):
                runner.run()
            assert invalidated and prefix is not None
            after = runner.snapshot
            assert after.performance == prefix.performance
            assert after.performance_cursor == prefix.performance_cursor
            assert after.performance_pending == prefix.performance_pending
            assert after.ledger == prefix.ledger
            assert not financial_records_after_loss
            assert not observations_after_loss
            with pytest.raises(ValueError, match="NOT_AVAILABLE"):
                runner.run()
            with pytest.raises(ValueError, match="NOT_AVAILABLE"):
                runner.report()


@pytest.mark.parametrize("fault", FAULTS)
def test_loss_at_run_closure_does_not_advance_or_publish(
    tmp_path: Path,
    database: Path,
    replay_request: ReplayRequest,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    reg = registration(tmp_path, start=START, count=int((END - START) // MINUTE))
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, START, END, VerificationMode.SEALED_SEGMENT_SESSION) as history:
        with SQLiteHistory(database) as trade:
            runner = ReplayRunner(
                trade,
                replay_request,
                ledger=FinancialLedger(),
                performance=FinancialPerformance(
                    policy(capital=None), marks=HistoricalMarkPriceSource(history, publication())
                ),
            )
            risk_tick = runner._risk_tick
            prefix: ReplaySnapshot | None = None

            def close_after_tick() -> None:
                nonlocal prefix
                risk_tick()
                if runner.snapshot.clock.now == START + MINUTE:
                    prefix = runner.snapshot
                    invalidate(history, Path(reg.original_file), fault, monkeypatch)

            monkeypatch.setattr(runner, "_risk_tick", close_after_tick)
            reason = {
                "prepared_close": "PREPARED_MARK_NOT_AVAILABLE",
                "session_close": "VERIFICATION_NOT_AVAILABLE",
                "ttl": "SESSION_EXPIRED",
                "source_changed": "SOURCE_CHANGED",
            }[fault]
            with pytest.raises(ValueError, match=reason):
                runner.run(START + MINUTE + timedelta(seconds=10))
            assert prefix is not None
            assert runner.snapshot == prefix
            with pytest.raises(ValueError, match="NOT_AVAILABLE"):
                runner.report()


@pytest.mark.parametrize("mode", list(VerificationMode))
def test_delivery_checks_do_not_reread_materialized_history(
    tmp_path: Path,
    database: Path,
    replay_request: ReplayRequest,
    monkeypatch: pytest.MonkeyPatch,
    mode: VerificationMode,
) -> None:
    reg = registration(tmp_path, start=START, count=int((END - START) // MINUTE))
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, START, END, mode) as history:
        with SQLiteHistory(database) as trade:
            runner = ReplayRunner(
                trade,
                replay_request,
                ledger=FinancialLedger(),
                performance=FinancialPerformance(
                    policy(capital=None), marks=HistoricalMarkPriceSource(history, publication())
                ),
            )

            def forbidden_read(*args: object, **kwargs: object) -> Never:
                raise AssertionError("No releer Mark preparado/original después de materializar")

            original_open = Path.open

            def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
                if self == Path(reg.original_file):
                    forbidden_read()
                return original_open(self, *args, **kwargs)

            monkeypatch.setattr(history, "read", forbidden_read)
            monkeypatch.setattr(Path, "open", guarded_open)
            report = runner.run()
            assert report.interruption is None
            assert runner.snapshot.performance_pending == ()
            assert runner.snapshot.performance is not None
            assert len(runner.snapshot.performance.events) == int((END - START) // MINUTE)
