"""Agenda, recuperación, fills reales del simulador y descarte del tick 5D4B."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest

from trading_engine.models.market import Candle
from trading_engine.replay.dynamic_funding import FixtureCalendarSource, FixtureFundingCalendar
from trading_engine.replay.dynamic_funding.core import DynamicFunding
from trading_engine.replay.dynamic_funding.models import FundingState
from trading_engine.replay.execution_model.quotes import ModeledQuotes
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import FundingEvidence
from trading_engine.replay.financial_performance.models import (
    FinancialObservation,
    PerformanceState,
)
from trading_engine.replay.historical_validation.funding_source import HistoricalFundingSource
from trading_engine.replay.historical_validation.verification import VerificationMode
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import EntryControl, FixtureFacts, ReplayFact
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution import SimulatedExecution

from .conftest import DUE, START, write_db
from .test_dynamic_funding import dynamic_policy
from .test_financial_performance import policy
from .test_historical_funding import inventory, prepared, publication, registered, registration, row
from .test_simulated_execution import simulation_policy
from .test_simulated_replay import round_candles


@contextmanager
def replay_component(
    folder: Path,
    candles: tuple[Candle, ...],
    *,
    mode: VerificationMode = VerificationMode.STRICT_FULL,
) -> Iterator[
    tuple[SQLiteHistory, ReplayRequest, FixtureFacts, DynamicFunding, FinancialPerformance]
]:
    rows = round_candles(candles)
    db = write_db(folder / "candles.sqlite3", rows)
    request = ReplayRequest(START, rows[-1].close_time, True)
    # Funding between real generated fills. No precomputed payments or position.
    minute = int((DUE - START).total_seconds() // 60) + 4
    rate_at = START + timedelta(minutes=minute, seconds=30)
    inv = inventory(start=START, end=request.end)  # replaced with explicit nonminute occurrence
    inv = replace(inv, expected_timestamps=(rate_at,))
    rate_row = row(minute, start=START, available=rate_at + timedelta(minutes=2))
    from trading_engine.binance.klines import to_ms

    rate_row["fundingTime"] = to_ms(rate_at)
    path, ids = registered(
        folder, registration(folder, [rate_row], inv, start=START, end=request.end)
    )
    with (
        SQLiteHistory(db) as history,
        prepared(path, ids, inv, mode, start=START, end=request.end) as funding,
    ):
        calendar = FixtureFundingCalendar(
            START,
            request.end,
            (rate_at,),
            request.end,
            funding.evidence.economic_identity,
            "Independent closed fixture schedule",
            "SYNTHETIC_FIXTURE_ONLY",
        )
        dynamic = DynamicFunding(
            HistoricalFundingSource(funding, publication()),
            dynamic_policy(),
            calendars=FixtureCalendarSource((calendar,)),
        )
        facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
        yield history, request, facts, dynamic, FinancialPerformance(policy(capital=None))


def runner_for(
    history: SQLiteHistory,
    request: ReplayRequest,
    facts: FixtureFacts,
    dynamic: DynamicFunding | None,
    performance: FinancialPerformance,
    snapshot: ReplaySnapshot | None = None,
) -> ReplayRunner:
    pricing = simulation_policy()
    pricing = replace(
        pricing, pricing=replace(pricing.pricing, quote_latency_ms=0), delivery_latency_ms=500
    )
    quotes = ModeledQuotes(history, pricing.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
    return ReplayRunner(
        history,
        request,
        facts=facts,
        quotes=quotes,
        simulator=SimulatedExecution(pricing, quotes),
        ledger=FinancialLedger(),
        performance=performance,
        dynamic_funding=dynamic,
        snapshot=snapshot,
    )


def test_v_ac_continuous_fragmented_and_restored_use_generated_fills(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    with replay_component(tmp_path, candles) as args:
        full = runner_for(*args)
        expected = full.run()
        assert expected.interruption is None
        assert expected.dynamic_funding is not None
        decision = expected.dynamic_funding.decisions[0]
        assert decision.status == "ASSIGNED" and decision.payment is not None
        assert decision.finalization is not None
        assert decision.finalization.kind == "SIMULATOR_GENERATION_AND_DELIVERY_BOUND"
        assert expected.financial_ledger is not None
        entries = expected.financial_ledger.entries
        effective = decision.rate_event.rate.market_effective_at
        with localcontext(ledger_context()):
            assert decision.quantity == sum(
                e.quantity if e.side.value == "BUY" else -e.quantity
                for e in entries
                if e.executed_at < effective
            )
        assert len(entries) > 4
        fragmented = runner_for(*args)
        for cut in (DUE, effective, decision.received_at, args[1].end):
            fragmented.run(cut)
            fragmented = runner_for(*args, snapshot=fragmented.snapshot)
        assert fragmented.report() == expected
        assert fragmented.snapshot == full.snapshot
        # Explicit opt-out remains byte-identical, including omission of optional fields.
        off = runner_for(args[0], args[1], args[2], None, args[4])
        off_report = off.run()
        assert off_report.dynamic_funding is None
        assert off.snapshot.funding is None and off.snapshot.funding_identity is None
        assert expected.financial_ledger == off_report.financial_ledger


@pytest.mark.parametrize("damage", ["cursor", "quantity", "journal", "proof", "session"])
def test_w_self_consistent_snapshot_checksum_cannot_authorize_funding(
    tmp_path: Path, candles: tuple[Candle, ...], damage: str
) -> None:
    with replay_component(tmp_path, candles) as args:
        runner = runner_for(*args)
        runner.run()
        snap = runner.snapshot
        state = snap.funding
        assert state is not None
        if damage == "session":
            snap = replace(snap, funding_session=object())
        elif damage == "cursor":
            snap = replace(snap, funding=replace(state, cursor=0))
        else:
            decision = state.journal[-1]
            if damage == "quantity":
                decision = replace(
                    decision, quantity=decision.quantity + 1 if decision.quantity else Decimal("1")
                )
            elif damage == "proof":
                decision = replace(decision, finalization=None)
            journal = () if damage == "journal" else state.journal[:-1] + (decision,)
            snap = replace(snap, funding=replace(state, journal=journal))
        snap = replace(snap, integrity_digest=snap.calculate_integrity())
        with pytest.raises(ValueError, match="Snapshot|sesión"):
            runner_for(*args, snapshot=snap)


@pytest.mark.parametrize("boundary", ["evidence", "observe", "value", "commit", "risk"])
def test_x_invalidation_discards_whole_current_tick_preserving_prefix(
    tmp_path: Path, candles: tuple[Candle, ...], monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    with replay_component(tmp_path, candles) as args:
        runner = runner_for(*args)
        rate = next(args[3].source.read(0, args[1].end))
        runner.run(rate.received_at - timedelta(milliseconds=1))
        before = runner.snapshot
        original_observe = FinancialPerformance.observe
        original_propose = DynamicFunding.propose
        original_risk = ReplayRunner._risk_tick
        original_check = DynamicFunding.check
        after_value = False

        def observe(
            self: FinancialPerformance, state: PerformanceState, observation: FinancialObservation
        ) -> PerformanceState:
            nonlocal after_value
            if boundary == "observe" and isinstance(observation.payload, type(rate)):
                raise AssertionError("Rate is not performance evidence")
            result = original_observe(self, state, observation)
            if observation.at == rate.received_at:
                if boundary == "observe" and isinstance(observation.payload, type(rate)):
                    raise AssertionError("Raw rate must never be observed")
                if (
                    boundary == "observe"
                    and getattr(observation.payload, "payload", None) is not None
                ):
                    args[3].source.history.close()
                if observation.payload == "VALUE":
                    if boundary == "value":
                        args[3].source.history.close()
                    after_value = True
            return result

        def propose(self: DynamicFunding, state: FundingState, context: Any) -> Any:
            result = original_propose(self, state, context)
            if boundary == "evidence" and context.at == rate.received_at:
                self.source.history.close()
            return result

        def risk(self: ReplayRunner) -> None:
            original_risk(self)
            if boundary == "risk" and self._snapshot.clock.now == rate.received_at:
                args[3].source.history.close()

        def check(self: DynamicFunding) -> None:
            if boundary == "commit" and after_value:
                self.source.history.close()
            original_check(self)

        monkeypatch.setattr(FinancialPerformance, "observe", observe)
        monkeypatch.setattr(DynamicFunding, "propose", propose)
        monkeypatch.setattr(ReplayRunner, "_risk_tick", risk)
        monkeypatch.setattr(DynamicFunding, "check", check)
        with pytest.raises(ValueError, match="PREPARED_FUNDING_NOT_AVAILABLE"):
            runner.run(rate.received_at)
        assert runner.snapshot == before
        assert runner.snapshot.ledger is not None and not runner.snapshot.ledger.conflicts
        with pytest.raises(ValueError, match="PREPARED_FUNDING_NOT_AVAILABLE"):
            runner.report()


def test_original_constructor_exception_not_replaced_by_trust_error(
    tmp_path: Path, candles: tuple[Candle, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_engine.replay.dynamic_funding import core

    with replay_component(tmp_path, candles) as args:
        runner = runner_for(*args)
        rate = next(args[3].source.read(0, args[1].end))
        runner.run(rate.received_at - timedelta(milliseconds=1))
        prefix = runner.snapshot
        error = ValueError("original-constructor-sentinel")

        def failing(*a: Any, **kw: Any) -> FundingEvidence:
            args[3].source.history.close()
            raise error

        monkeypatch.setattr(core, "FundingEvidence", failing)
        with pytest.raises(ValueError) as caught:
            runner.run(rate.received_at)
        assert caught.value is error
        assert runner.snapshot == prefix
