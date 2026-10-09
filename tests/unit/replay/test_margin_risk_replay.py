"""Integración causal, reanudación íntegra y compatibilidad de la fase anterior."""

import ast
import json
from collections.abc import Generator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path

import pytest

from trading_engine.models.enums import Side
from trading_engine.models.market import Candle
from trading_engine.replay.encoding import encoded
from trading_engine.replay.execution_model.quotes import ModeledQuotes
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import FixtureFinancialSource
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.isolated_margin_risk import IsolatedMarginRisk
from trading_engine.replay.isolated_margin_risk.evidence import (
    FixtureRiskSource,
    MarginAdjustmentEvidence,
)
from trading_engine.replay.models import ReplayReport, ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import EntryControl, FixtureFacts, FixtureQuotes, ReplayFact
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import SimulationPolicy
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, END, START, write_db
from .test_financial_performance import FIXTURE, policy
from .test_financial_replay import accounting_runner
from .test_isolated_margin_risk import margin_policy, schedule, stamp
from .test_performance_replay import costs_for, runner_for
from .test_simulated_execution import simulation_policy
from .test_simulated_replay import round_candles


def risk_runner(
    history: SQLiteHistory,
    pricing: SimulationPolicy,
    request: ReplayRequest,
    facts: FixtureFacts,
    performance: FinancialPerformance,
    risk: IsolatedMarginRisk,
    snapshot: ReplaySnapshot | None = None,
) -> ReplayRunner:
    quotes = ModeledQuotes(history, pricing.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
    return ReplayRunner(
        history,
        request,
        quotes=quotes,
        facts=facts,
        simulator=SimulatedExecution(pricing, quotes),
        ledger=FinancialLedger(),
        performance=performance,
        risk=risk,
        snapshot=snapshot,
    )


@dataclass(frozen=True)
class RiskRound:
    history: SQLiteHistory
    pricing: SimulationPolicy
    request: ReplayRequest
    facts: FixtureFacts
    performance: FinancialPerformance
    risk: IsolatedMarginRisk
    seed: ReplaySnapshot
    expected: ReplayReport
    final: ReplaySnapshot
    baseline: ReplayReport


@pytest.fixture(scope="module")
def risk_round(
    tmp_path_factory: pytest.TempPathFactory, candles: tuple[Candle, ...]
) -> Generator[RiskRound, None, None]:
    rows = round_candles(candles, old_tp_fill=True)
    database = write_db(tmp_path_factory.mktemp("margin-risk-round") / "fixture.sqlite3", rows)
    pricing = simulation_policy()
    pricing = replace(
        pricing, pricing=replace(pricing.pricing, quote_latency_ms=0), delivery_latency_ms=500
    )
    request = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(database) as history:
        gross = accounting_runner(history, pricing, request, facts).run()
        assert gross.financial_ledger is not None and gross.interruption is None
        performance = costs_for(gross)
        baseline = runner_for(history, pricing, request, facts, performance).run()
        rid = gross.financial_ledger.entries[0].round_id
        at = DUE + timedelta(minutes=5, seconds=10)
        addition = MarginAdjustmentEvidence(
            stamp("synthetic-addition", at=at, sequence=1), rid, Decimal("100"), "ADD"
        )
        synthetic = replace(schedule(at=START), valid_until=request.end + timedelta(minutes=1))
        risk = IsolatedMarginRisk(margin_policy(), FixtureRiskSource((synthetic, addition)))
        runner = risk_runner(history, pricing, request, facts, performance, risk)
        runner.run(DUE)
        seed = runner.snapshot
        expected = runner.run()
        assert expected.interruption is None, expected.interruption
        yield RiskRound(
            history,
            pricing,
            request,
            facts,
            performance,
            risk,
            seed,
            expected,
            runner.snapshot,
            baseline,
        )


def test_synthetic_base_seven_dca_partial_old_tp_cancel_replace_and_close(
    risk_round: RiskRound,
) -> None:
    report = risk_round.expected
    assert report.final_state == risk_round.baseline.final_state
    assert report.simulation == risk_round.baseline.simulation
    assert report.financial_ledger == risk_round.baseline.financial_ledger
    assert report.order_intents == risk_round.baseline.order_intents
    assert report.cancel_requests == risk_round.baseline.cancel_requests
    assert (
        report.financial_performance is not None
        and risk_round.baseline.financial_performance is not None
    )
    assert report.financial_performance.current == risk_round.baseline.financial_performance.current
    risk = report.isolated_margin_risk
    assert risk is not None and not risk.conflicts
    assert report.financial_ledger is not None
    raw = report.financial_ledger
    final = risk.current.positions[0]
    assert final.fill_ids == tuple(e.fill_id for e in raw.entries)
    assert final.open_quantity == 0 and final.condition == "NO_OPEN_POSITION"
    assert final.maintenance.value == 0 and final.threshold_price.value is None
    assert final.additional_assigned.value == Decimal("100")
    assert final.initial_assigned.value == final.initial_released.value
    assert final.transitions[-1].initial_principal_after_usdc == 0
    with localcontext(ledger_context()):
        assigned = sum(
            (e.notional_usdc / Decimal("4") for e in raw.entries if e.side is Side.BUY),
            Decimal("0"),
        )
        assert final.initial_assigned.value == assigned
        assert final.allocated_fees.value is not None and final.allocated_funding.value is not None
        assert (
            final.isolated_wallet_balance.value
            == Decimal("100") - final.allocated_fees.value + final.allocated_funding.value
        )
    strategy = report.final_state.strategy_state
    assert isinstance(strategy, AltBStrategyState)
    r = strategy.round_history[0]
    assert r.round_state.status(r.base) == "CLOSED"
    assert all(leg.fills for leg in r.round_state.dcas) and len(r.round_state.dcas) == 7
    assert len(r.round_state.dcas[0].fills) > 1
    assert any(len(tp.fills) > 1 for tp in r.round_state.take_profits)
    assert len(r.round_state.take_profits) > 1
    assert any(
        p.open_quantity > 0 and p.availability == "MODELED"
        for v in risk.evaluations
        for p in v.positions
    )
    assert risk.current.survival == "NOT_DETERMINABLE_FROM_SAMPLES"
    assert risk.runtime == "STOPPED"


@pytest.mark.parametrize(
    "cut",
    [
        DUE,
        DUE + timedelta(minutes=2, milliseconds=500),
        DUE + timedelta(minutes=5, seconds=9),
        DUE + timedelta(minutes=5, seconds=10),
        DUE + timedelta(minutes=6),
        DUE + timedelta(minutes=12, milliseconds=333),
    ],
)
def test_continuous_equals_fragmented_exactly_including_pending_adjustments(
    risk_round: RiskRound, cut: datetime
) -> None:
    r = risk_round
    part = risk_runner(r.history, r.pricing, r.request, r.facts, r.performance, r.risk, r.seed)
    part.run(cut)
    checkpoint = part.snapshot
    if cut < DUE + timedelta(minutes=5, seconds=10):
        assert checkpoint.risk_pending
    restored = risk_runner(
        r.history, r.pricing, r.request, r.facts, r.performance, r.risk, checkpoint
    )
    assert restored.run() == r.expected
    assert restored.snapshot == r.final


@pytest.mark.parametrize("change", ["policy", "dataset", "schedule", "version"])
def test_restore_different_risk_identity_rejected(risk_round: RiskRound, change: str) -> None:
    r = risk_round
    if change == "policy":
        changed = replace(r.risk, policy=replace(r.risk.policy, threshold_solver=False))
    elif change == "version":
        with pytest.raises(ValueError):
            replace(r.risk.policy, rules_version="unimplemented")
        return
    else:
        assert isinstance(r.risk.source, FixtureRiskSource)
        first, last = r.risk.source.events
        if change == "dataset":
            first = replace(
                first, stamp=replace(first.stamp, reference=replace(FIXTURE, dataset="different"))
            )
        else:
            from trading_engine.replay.isolated_margin_risk.evidence import (
                MaintenanceBracketSchedule,
            )

            assert isinstance(first, MaintenanceBracketSchedule)
            first = replace(
                first, brackets=(replace(first.brackets[0], maintenance_rate=Decimal(".02")),)
            )
        changed = replace(r.risk, source=FixtureRiskSource((first, last)))
    with pytest.raises(ValueError):
        risk_runner(r.history, r.pricing, r.request, r.facts, r.performance, changed, r.seed)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "series",
        "journal",
        "events",
        "context",
        "cursor",
        "pending",
        "self_consistent_history",
    ],
)
def test_tampering_rejected_after_recalculated_checksum(risk_round: RiskRound, damage: str) -> None:
    r = risk_round
    snap = r.final if damage not in {"cursor", "pending"} else r.seed
    assert snap.risk is not None
    state = snap.risk
    if damage == "missing":
        snap = replace(snap, risk=None)
    elif damage == "cursor":
        snap = replace(snap, risk_cursor=0)
    elif damage == "pending":
        snap = replace(snap, risk_pending=())
    elif damage == "series":
        snap = replace(snap, risk=replace(state, evaluations=()))
    elif damage == "journal":
        snap = replace(snap, risk=replace(state, journal=()))
    elif damage == "events":
        snap = replace(snap, risk=replace(state, events=()))
    elif damage == "context":
        assert state.context is not None
        snap = replace(
            snap, risk=replace(state, context=replace(state.context, performance_journal_cursor=0))
        )
    else:
        # Omitir un punto Y las transiciones asociadas deja una historia interna
        # auto-consistente: la reconstrucción autoritativa debe igualmente fallar.
        point = state.evaluations[3]
        state = replace(
            state,
            evaluations=tuple(v for v in state.evaluations if v.at != point.at),
            journal=tuple(v for v in state.journal if v.at != point.at),
        )
        snap = replace(snap, risk=state)
    snap = replace(snap, integrity_digest=snap.calculate_integrity())
    with pytest.raises(ValueError):
        risk_runner(r.history, r.pricing, r.request, r.facts, r.performance, r.risk, snap)


def test_approved_performance_enabled_baseline_bytes_unchanged_without_risk(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    rows = round_candles(candles)
    database = write_db(tmp_path / "compat.sqlite3", rows)
    baseline = json.loads(
        (Path(__file__).parent / "fixtures/isolated_margin_disabled_baseline.json").read_text()
    )
    pricing = simulation_policy()
    pricing = replace(pricing, pricing=replace(pricing.pricing, quote_latency_ms=0))
    with SQLiteHistory(database) as history:
        runner = runner_for(
            history,
            pricing,
            ReplayRequest(START, END, True),
            FixtureFacts(),
            FinancialPerformance(policy()),
        )
        report = runner.run()
        assert report.event_digest == baseline["event_digest"]
        assert runner.snapshot.integrity_digest == baseline["integrity_digest"]
        assert sha256(encoded(report)).hexdigest() == baseline["report_digest"]
        assert "isolated_margin_risk" not in report.as_dict()


@pytest.mark.parametrize("volume", ["0", "1"])
def test_shortfill_keeps_economic_exposure_while_strategy_blocked(
    tmp_path: Path, candles: tuple[Candle, ...], volume: str
) -> None:
    at = DUE + timedelta(minutes=2)
    rows = tuple(
        replace(c, volume=Decimal(volume), taker_buy_base_volume=Decimal(volume) / Decimal("2"))
        if c.close_time == at
        else c
        for c in round_candles(candles)
    )
    database = write_db(tmp_path / "shortfill.sqlite3", rows)
    pricing = simulation_policy()
    pricing = replace(pricing, pricing=replace(pricing.pricing, quote_latency_ms=0))
    with SQLiteHistory(database) as history:
        report = risk_runner(
            history,
            pricing,
            ReplayRequest(START, END, True),
            FixtureFacts(),
            FinancialPerformance(policy()),
            IsolatedMarginRisk(margin_policy()),
        ).run()
    assert report.interruption is None and report.order_intents == 1
    state = report.final_state.strategy_state
    assert isinstance(state, AltBStrategyState) and state.base is not None
    assert state.base.p0 is None and state.round_state is None
    risk = report.isolated_margin_risk
    assert risk is not None
    if volume == "1":
        v = risk.current.positions[0]
        assert state.base.block_reason == "base_shortfill_reconciliation"
        assert v.open_quantity == Decimal(".05")
        assert v.initial_assigned.value is not None and v.initial_assigned.value > 0
        assert v.initial_released.value == 0 and v.condition == "UNKNOWN"
    else:
        assert risk.current.positions == ()


def test_noquotes_and_fixturequotes_require_opt_in_and_no_lookahead(
    database: Path, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    risk = IsolatedMarginRisk(margin_policy(), FixtureRiskSource((schedule(at=START),)))
    performance = FinancialPerformance(policy())
    request = ReplayRequest(START, END, True)
    with SQLiteHistory(database) as history:
        with pytest.raises(ValueError):
            ReplayRunner(history, request, risk=risk)
        runner = ReplayRunner(
            history, request, ledger=FinancialLedger(), performance=performance, risk=risk
        )
        assert runner.run().order_intents == 0
        runner = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=FixtureFacts((base_fact,)),
            ledger=FinancialLedger(),
            performance=performance,
            risk=risk,
        )
        early = runner.run(DUE)
        assert (
            early.isolated_margin_risk is not None
            and early.isolated_margin_risk.current.positions == ()
        )
        report = runner.run()
        assert report.interruption is None and report.isolated_margin_risk is not None
        assert report.isolated_margin_risk.current.positions[0].condition == "UNKNOWN"
        assert all(v.at <= DUE for v in early.isolated_margin_risk.evaluations)


def test_offline_no_network_no_execution_no_strategy_imports(
    risk_round: RiskRound, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket
    import urllib.request

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline only")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    r = risk_round
    assert (
        risk_runner(r.history, r.pricing, r.request, r.facts, r.performance, r.risk, r.seed).run()
        == r.expected
    )
    root = Path(__file__).resolve().parents[3] / "src/trading_engine/replay/isolated_margin_risk"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                name.split(".")[0] in {"socket", "urllib", "requests", "http", "time", "subprocess"}
                or name.startswith(
                    (
                        "trading_engine.live",
                        "trading_engine.execution",
                        "trading_engine.strategy",
                        "trading_engine.binance",
                    )
                )
                for name in names
            )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"now", "submit", "poll", "sleep"}


def test_warmup_checkpoint_and_sqlite_session_identity(database: Path) -> None:
    performance = FinancialPerformance(policy())
    risk = IsolatedMarginRisk(margin_policy())
    request = ReplayRequest(START, END)
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history, request, ledger=FinancialLedger(), performance=performance, risk=risk
        )
        runner.run(START - timedelta(minutes=1))
        checkpoint = runner.snapshot
        restored = ReplayRunner(
            history,
            request,
            ledger=FinancialLedger(),
            performance=performance,
            risk=risk,
            snapshot=checkpoint,
        )
        assert restored.run() == runner.run()
    with SQLiteHistory(database) as other:
        with pytest.raises(ValueError):
            ReplayRunner(
                other,
                request,
                ledger=FinancialLedger(),
                performance=performance,
                risk=risk,
                snapshot=checkpoint,
            )


@pytest.mark.parametrize("contradict", [False, True])
def test_duplicate_adjustment_restore_or_conflict_preserves_financial_ledger(
    database: Path,
    quotes: FixtureQuotes,
    base_fact: ReplayFact,
    contradict: bool,
) -> None:
    from trading_engine.replay.ports import FillEvidence

    assert isinstance(base_fact.payload, FillEvidence)
    rid = base_fact.payload.order.intent.round_id
    first = MarginAdjustmentEvidence(
        stamp("adjust", at=base_fact.received_at), rid, Decimal("10"), "ADD"
    )
    duplicate = replace(
        first,
        stamp=replace(
            first.stamp, received_at=first.stamp.received_at + timedelta(seconds=1), sequence=1
        ),
        amount_usdc=Decimal("11" if contradict else "10"),
    )
    risk = IsolatedMarginRisk(margin_policy(), FixtureRiskSource((first, duplicate)))
    performance = FinancialPerformance(policy())
    request = ReplayRequest(START, END, True)
    facts = FixtureFacts((base_fact,))
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
            risk=risk,
        )
        report = runner.run()
        assert report.financial_ledger is not None and not report.financial_ledger.conflicts
        assert (
            report.financial_performance is not None and not report.financial_performance.conflicts
        )
        assert report.isolated_margin_risk is not None
        assert bool(report.interruption) == contradict
        assert bool(report.isolated_margin_risk.conflicts) == contradict
        assert len(report.isolated_margin_risk.processed_event_ids) == 1
        restored = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
            risk=risk,
            snapshot=runner.snapshot,
        )
        assert restored.run() == report
        if contradict:
            assert report.isolated_margin_risk.current.positions[0].buffer.status == "BLOCKED"
            altered = replace(runner.snapshot, risk_cursor=0, risk_pending=risk.source.events)  # type: ignore[attr-defined]
            altered = replace(altered, integrity_digest=altered.calculate_integrity())
            with pytest.raises(ValueError):
                ReplayRunner(
                    history,
                    request,
                    quotes=quotes,
                    facts=facts,
                    ledger=FinancialLedger(),
                    performance=performance,
                    risk=risk,
                    snapshot=altered,
                )


def test_financial_conflict_propagates_to_risk_without_inventing_exposure(
    database: Path, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    from uuid import UUID

    from trading_engine.models.ids import OrderId
    from trading_engine.replay.financial_performance.evidence import FeeEvidence, FinancialEvent
    from trading_engine.replay.ports import FillEvidence

    assert isinstance(base_fact.payload, FillEvidence)
    fill = base_fact.payload.fill
    fee = FeeEvidence(
        "bad-fee",
        fill.fill_id,
        OrderId(UUID(int=999)),
        fill.timestamp,
        Decimal("1"),
        "USDC",
        FIXTURE,
    )
    performance = FinancialPerformance(
        policy(),
        FixtureFinancialSource(
            (FinancialEvent(base_fact.received_at + timedelta(seconds=1), 0, fee),)
        ),
    )
    risk = IsolatedMarginRisk(margin_policy())
    request = ReplayRequest(START, END, True)
    facts = FixtureFacts((base_fact,))
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
            risk=risk,
        )
        report = runner.run()
        assert report.interruption and "financial_performance" in report.interruption
        assert report.financial_ledger is not None and not report.financial_ledger.conflicts
        assert report.isolated_margin_risk is not None and report.isolated_margin_risk.conflicts
        assert report.isolated_margin_risk.current.positions[0].open_quantity == fill.quantity.value
        assert report.isolated_margin_risk.current.positions[0].buffer.status == "BLOCKED"
        restored = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
            risk=risk,
            snapshot=runner.snapshot,
        )
        assert restored.run() == report


@pytest.mark.parametrize("condition", ["gap", "corrupt"])
def test_market_gaps_and_corrupt_storage_never_manufacture_mark(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    condition: str,
) -> None:
    import sqlite3

    from trading_engine.binance.klines import minute_ms

    rows = tuple(c for c in candles if c.close_time != DUE)
    database = write_db(tmp_path / "gaps.sqlite3", rows)
    if condition == "corrupt":
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE futures_klines_1m SET high = '90' WHERE open_time_ms = ?",
                (minute_ms(START),),
            )
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history,
            ReplayRequest(START, END, True),
            ledger=FinancialLedger(),
            performance=FinancialPerformance(policy()),
            risk=IsolatedMarginRisk(margin_policy()),
        )
        report = runner.run()
        if condition == "corrupt":
            assert report.interruption is not None
            assert report.isolated_margin_risk is not None and report.isolated_margin_risk.conflicts
        assert report.order_intents == 0
        assert report.isolated_margin_risk is not None
        assert not report.isolated_margin_risk.current.positions
        assert report.isolated_margin_risk.current.survival == "NOT_DETERMINABLE_FROM_SAMPLES"
