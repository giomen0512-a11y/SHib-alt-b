"""Mismo runner/motor, costos offline, snapshots y compatibilidad byte a byte."""

import ast
import json
from collections.abc import Generator
from dataclasses import replace
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
from trading_engine.replay.financial_performance.comparison import compare_scenarios
from trading_engine.replay.financial_performance.evidence import (
    FeeEvidence,
    FinancialEvent,
    FixtureFinancialSource,
    FixtureMarks,
    FundingEvidence,
)
from trading_engine.replay.financial_performance.policy import FeeRatePolicy
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayReport, ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import EntryControl, FixtureFacts, FixtureQuotes, ReplayFact
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import SimulationPolicy
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, END, START, write_db
from .test_financial_performance import FIXTURE, MODELED, coverage, mark, policy
from .test_financial_replay import accounting_runner
from .test_simulated_execution import simulation_policy
from .test_simulated_replay import round_candles


def runner_for(
    history: SQLiteHistory,
    pricing: SimulationPolicy,
    request: ReplayRequest,
    facts: FixtureFacts,
    performance: FinancialPerformance,
    snapshot: ReplaySnapshot | None = None,
) -> ReplayRunner:
    quotes = ModeledQuotes(history, pricing.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
    simulator = SimulatedExecution(pricing, quotes)
    return ReplayRunner(
        history,
        request,
        quotes=quotes,
        facts=facts,
        simulator=simulator,
        ledger=FinancialLedger(),
        performance=performance,
        snapshot=snapshot,
    )


def costs_for(report: ReplayReport, *, delayed_ms: int = 200) -> FinancialPerformance:
    ledger = report.financial_ledger
    assert ledger is not None
    events: list[FinancialEvent] = []
    # Tasas solo verifican las ecuaciones: cada fill recibe FeeEvidence explícita.
    with localcontext(ledger_context()):
        for i, entry in enumerate(ledger.entries):
            fee = FeeEvidence(
                "fee:" + str(entry.fill_id),
                entry.fill_id,
                entry.order_id,
                entry.executed_at,
                entry.notional_usdc * Decimal(".001"),
                "USDC",
                FIXTURE,
            )
            events.append(
                FinancialEvent(entry.received_at + timedelta(milliseconds=delayed_ms), i, fee)
            )
        for index, at in enumerate(
            (DUE + timedelta(minutes=3, seconds=30), DUE + timedelta(minutes=10, seconds=30))
        ):
            entries = [e for e in ledger.entries if e.executed_at < at]
            buy = sum(
                (
                    e.quantity
                    for e in sorted(entries, key=lambda e: e.fill_id.int)
                    if e.side is Side.BUY
                ),
                Decimal("0"),
            )
            sell = sum(
                (
                    e.quantity
                    for e in sorted(entries, key=lambda e: e.fill_id.int)
                    if e.side is Side.SELL
                ),
                Decimal("0"),
            )
            f = FundingEvidence(
                "funding:" + str(index),
                ledger.round_ledger[0].symbol,
                at,
                ledger.round_ledger[0].round_id,
                buy - sell,
                FIXTURE,
                amount_usdc=Decimal("-3" if index == 0 else "1"),
            )
            events.append(FinancialEvent(at + timedelta(milliseconds=delayed_ms), len(events), f))
    marks: list[FinancialEvent] = []
    at = START + timedelta(minutes=1)
    while at <= report.requested_end:
        ids = tuple(
            e.payload.event_id
            for e in events
            if isinstance(e.payload, FundingEvidence) and e.payload.effective_at <= at
        )
        for payload in (coverage("FUNDING", at=at, ids=ids), coverage("TRANSFERS", at=at)):
            events.append(FinancialEvent(at, len(events), payload))
        marks.append(FinancialEvent(at, len(marks), mark("95", at=at)))
        at += timedelta(minutes=1)
    return FinancialPerformance(
        policy(capital="3200"), FixtureFinancialSource(tuple(events)), FixtureMarks(tuple(marks))
    )


@pytest.fixture(scope="module")
def performance_round(
    tmp_path_factory: pytest.TempPathFactory, candles: tuple[Candle, ...]
) -> Generator[
    tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        FinancialPerformance,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    None,
    None,
]:
    rows = round_candles(candles)
    database = write_db(tmp_path_factory.mktemp("performance-round") / "fixture.sqlite3", rows)
    pricing = simulation_policy()
    pricing = replace(
        pricing, pricing=replace(pricing.pricing, quote_latency_ms=0), delivery_latency_ms=500
    )
    request = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(database) as history:
        baseline = accounting_runner(history, pricing, request, facts).run()
        assert baseline.interruption is None
        performance = costs_for(baseline)
        runner = runner_for(history, pricing, request, facts, performance)
        runner.run(DUE)
        seed = runner.snapshot
        expected = runner.run()
        yield history, pricing, request, facts, performance, seed, expected, runner.snapshot


def test_full_base_dca7_tp_closed_with_independent_financial_reconciliation(
    performance_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        FinancialPerformance,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
) -> None:
    _, _, _, _, performance, _, report, final = performance_round
    assert report.interruption is None, report.interruption
    gross, financial = report.financial_ledger, report.financial_performance
    assert gross is not None and financial is not None
    assert report.simulation is not None
    state = report.final_state.strategy_state
    assert isinstance(state, AltBStrategyState)
    round_state = state.round_history[0].round_state
    assert round_state.status(state.round_history[0].base) == "CLOSED"
    assert all(leg.fills for leg in round_state.dcas)
    assert len(round_state.dcas[0].fills) > 1
    assert len(round_state.take_profits) > 1
    assert any(len(tp.fills) > 1 for tp in round_state.take_profits)
    assert (
        report.execution_metrics is not None
        and report.execution_metrics.cancellations_confirmed > 0
    )
    assert gross.open_quantity == gross.open_cost_basis_usdc == 0
    current = financial.current
    with localcontext(ledger_context()):
        fee_sum = sum(
            (
                e.notional_usdc * Decimal(".001")
                for e in sorted(gross.entries, key=lambda e: e.fill_id.int)
            ),
            Decimal("0"),
        )
        net = gross.gross_realized_pnl_usdc - fee_sum - Decimal("2")
        assert current.fees.value == fee_sum
        assert current.funding.value == Decimal("-2")
        assert current.net_cash.value == net
        assert current.wallet.value == Decimal("3200") + net
        assert current.unrealized.value == 0
        assert current.equity.value == current.wallet.value
        assert current.rounds[0].net_realized_closed.value == net
    assert financial.valuation_coverage == "PARTIAL"
    assert any(
        point.unrealized.value is not None and point.unrealized.value < 0
        for point in financial.series
    )
    assert financial.max_drawdown_usdc.value is not None and financial.max_drawdown_usdc.value > 0
    assert current.equity.status == "MODELED"
    assert not financial.conflicts
    assert final.performance is not None and final.ledger is not None
    performance.validate(final.performance, report.processed_until, final.ledger)
    assert final.performance_pending == ()
    assert (
        financial.general_return_pct
        == financial.margin_available
        == financial.liquidation
        == "NOT_AVAILABLE"
    )


@pytest.mark.parametrize(
    "cut",
    [
        DUE,
        DUE + timedelta(minutes=2),
        DUE + timedelta(minutes=2, milliseconds=500),
        DUE + timedelta(minutes=2, milliseconds=600),
        DUE + timedelta(minutes=5, seconds=30),
        DUE + timedelta(minutes=6),
        DUE + timedelta(minutes=16),
    ],
)
def test_continuous_equals_fragmented_every_financial_state_and_fingerprint(
    performance_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        FinancialPerformance,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    cut: datetime,
) -> None:
    history, pricing, request, facts, performance, seed, expected, final = performance_round
    part = runner_for(history, pricing, request, facts, performance, seed)
    part.run(cut)
    checkpoint = part.snapshot
    assert checkpoint.performance_pending
    restored = runner_for(history, pricing, request, facts, performance, checkpoint)
    assert restored.run() == expected
    assert restored.snapshot == final


@pytest.mark.parametrize("change", ["policy", "dataset", "marks", "capital", "version"])
def test_restore_different_financial_configuration_rejected(
    performance_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        FinancialPerformance,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    change: str,
) -> None:
    history, pricing, request, facts, performance, seed, _, _ = performance_round
    if change == "policy":
        changed = replace(performance, policy=replace(performance.policy, scenario="ADVERSO"))
    elif change == "dataset":
        first, *rest = performance.source.events  # type: ignore[attr-defined]
        altered = replace(
            first, payload=replace(first.payload, reference=replace(FIXTURE, dataset="different"))
        )
        changed = replace(performance, source=FixtureFinancialSource((altered, *rest)))
    elif change == "marks":
        changed = replace(performance, marks=FixtureMarks())
    elif change == "capital":
        assert performance.policy.capital is not None
        changed = replace(
            performance,
            policy=replace(
                performance.policy,
                capital=replace(performance.policy.capital, balance_usdc=Decimal("1")),
            ),
        )
    else:
        with pytest.raises(ValueError):
            replace(performance.policy, rules_version="future")
        return
    with pytest.raises(ValueError):
        runner_for(history, pricing, request, facts, changed, seed)


@pytest.mark.parametrize(
    "damage", ["performance", "series", "cursor", "pending", "ledger", "conflict"]
)
def test_recovery_tampered_snapshot_even_with_recomputed_checksum(
    performance_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        FinancialPerformance,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    damage: str,
) -> None:
    history, pricing, request, facts, performance, seed, _, final = performance_round
    snapshot = final if damage == "series" else seed
    assert snapshot.performance is not None
    performance_state = snapshot.performance
    if damage == "performance":
        snapshot = replace(snapshot, performance=None)
    elif damage == "series":
        snapshot = replace(snapshot, performance=replace(performance_state, series=()))
    elif damage == "cursor":
        snapshot = replace(snapshot, performance_cursor=0)
    elif damage == "pending":
        snapshot = replace(snapshot, performance_pending=())
    elif damage == "ledger":
        assert final.performance is not None
        snapshot = replace(
            snapshot, performance=replace(performance_state, ledger=final.performance.ledger)
        )
    else:
        snapshot = replace(snapshot, performance_identity="other")
    snapshot = replace(snapshot, integrity_digest=snapshot.calculate_integrity())
    with pytest.raises(ValueError):
        runner_for(history, pricing, request, facts, performance, snapshot)


def test_prior_ledger_enabled_fingerprint_frozen_from_approved_main(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    database = write_db(tmp_path / "compat.sqlite3", round_candles(candles))
    baseline = json.loads(
        (
            Path(__file__).parent / "fixtures/financial_performance_disabled_baseline.json"
        ).read_text()
    )
    pricing = simulation_policy()
    pricing = replace(pricing, pricing=replace(pricing.pricing, quote_latency_ms=0))
    with SQLiteHistory(database) as history:
        runner = accounting_runner(
            history, pricing, ReplayRequest(START, END, True), FixtureFacts()
        )
        report = runner.run()
        assert report.financial_performance is None
        assert runner.snapshot.performance is None
        assert report.event_digest == baseline["event_digest"]
        assert runner.snapshot.integrity_digest == baseline["integrity_digest"]
        assert sha256(encoded(report)).hexdigest() == baseline["report_digest"]
        assert "financial_performance" not in report.as_dict()


@pytest.mark.parametrize("volume", ["0", "1"])
def test_base_shortfill_keeps_exposure_and_never_activates_dca_tp(
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
    request = ReplayRequest(START, DUE + timedelta(minutes=5), True)
    with SQLiteHistory(database) as history:
        runner = runner_for(
            history, pricing, request, FixtureFacts(), FinancialPerformance(policy())
        )
        report = runner.run()
        assert report.interruption is None
        assert report.financial_performance is not None and report.financial_ledger is not None
        state = report.final_state.strategy_state
        assert isinstance(state, AltBStrategyState) and state.base is not None
        assert state.round_state is None and state.base.p0 is None
        assert state.base.block_reason != "base_confirmed"
        assert report.order_intents == 1
        financial = report.financial_performance.current
        if volume == "1":
            assert state.base.block_reason == "base_shortfill_reconciliation"
            assert report.financial_ledger.open_quantity == Decimal(".05")
            assert financial.unrealized.value is None
            assert financial.equity.value is None
        else:
            assert report.financial_ledger.open_quantity == 0
            assert financial.unrealized.value == 0


def test_noquotes_extended_accounting_has_no_execution_and_requires_opt_in(database: Path) -> None:
    with SQLiteHistory(database) as history:
        with pytest.raises(ValueError):
            ReplayRunner(
                history, ReplayRequest(START, END), performance=FinancialPerformance(policy())
            )
        runner = ReplayRunner(
            history,
            ReplayRequest(START, END, True),
            ledger=FinancialLedger(),
            performance=FinancialPerformance(policy()),
        )
        report = runner.run()
        assert report.order_intents == 0 and report.financial_ledger is not None
        assert report.financial_performance is not None
        assert report.financial_ledger.entries == ()
        assert report.financial_performance.current.unrealized.value == 0
        assert report.financial_performance.current.equity.value is None


def test_fixturequotes_and_fee_delivery_do_not_anticipate_costs(
    database: Path, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    from trading_engine.replay.ports import FillEvidence

    assert isinstance(base_fact.payload, FillEvidence)
    fill, order = base_fact.payload.fill, base_fact.payload.order
    at = base_fact.received_at + timedelta(seconds=1)
    fee = FeeEvidence(
        "fee", fill.fill_id, order.order_id, fill.timestamp, Decimal("1.23456789"), "USDC", FIXTURE
    )
    performance = FinancialPerformance(
        policy(), FixtureFinancialSource((FinancialEvent(at, 0, fee),))
    )
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history,
            ReplayRequest(START, END, True),
            quotes=quotes,
            facts=FixtureFacts((base_fact,)),
            ledger=FinancialLedger(),
            performance=performance,
        )
        report = runner.run(base_fact.received_at)
        assert report.financial_performance is not None
        assert report.financial_performance.current.fees.value is None
        original_series = report.financial_performance.series
        report = runner.run(at)
        assert report.financial_performance is not None
        assert report.financial_performance.current.fees.value == Decimal("1.23456789")
        assert report.financial_performance.series[: len(original_series)] == original_series


@pytest.mark.parametrize("scenario", ["FAVORABLE", "BASE", "ADVERSO"])
def test_three_scenarios_explicit_and_reproducible(
    performance_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        FinancialPerformance,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    scenario: str,
) -> None:
    _, _, _, _, p, _, _, final = performance_round
    assert final.ledger is not None and final.performance is not None
    reports = []
    for name, rate in (("FAVORABLE", ".0005"), ("BASE", ".001"), ("ADVERSO", ".002")):
        rates = FeeRatePolicy(
            Decimal(rate),
            Decimal(rate),
            tuple((e.fill_id, "TAKER") for e in final.ledger.entries),
            MODELED,
        )
        changed = replace(p, policy=replace(p.policy, scenario=name, fees=rates))
        assert isinstance(p.source, FixtureFinancialSource)
        changed = replace(
            changed,
            source=FixtureFinancialSource(
                tuple(e for e in p.source.events if not isinstance(e.payload, FeeEvidence))
            ),
        )
        state = changed.initial(START, final.request.end)
        # Reproducir el journal anterior cambiando solamente el costo explícito.
        for observation in final.performance.journal:
            if isinstance(observation.payload, FinancialEvent) and isinstance(
                observation.payload.payload, FeeEvidence
            ):
                continue
            state = changed.observe(state, observation)
        assert not state.conflicts
        reports.append(changed.report(state, final.clock.now))
    comparison = compare_scenarios(tuple(reports))
    assert comparison == compare_scenarios(tuple(reversed(reports)))
    selected = next(r for r in comparison.results if r.scenario == scenario)
    assert selected.equity.status == "MODELED"
    favorable, base, adverse = comparison.results
    assert (
        favorable.fees.value is not None
        and base.fees.value is not None
        and adverse.fees.value is not None
    )
    assert favorable.fees.value < base.fees.value < adverse.fees.value
    with pytest.raises(ValueError):
        compare_scenarios((reports[0], reports[0], reports[2]))
    assert reports[0].policy.fees is not None
    wrong = replace(
        reports[0],
        policy=replace(
            reports[0].policy, fees=replace(reports[0].policy.fees, maker_rate=Decimal("1"))
        ),
    )
    with pytest.raises(ValueError):
        compare_scenarios((wrong, reports[1], reports[2]))


def test_offline_no_network_no_live_no_strategy_import(
    performance_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        FinancialPerformance,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import urllib.request

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Financial performance offline no network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    history, pricing, request, facts, performance, seed, expected, _ = performance_round
    assert runner_for(history, pricing, request, facts, performance, seed).run() == expected
    root = Path(__file__).resolve().parents[3] / "src/trading_engine/replay/financial_performance"
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


def test_old_tp_partial_before_cancel_confirmation_keeps_net_round_consistent(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    rows = round_candles(candles, old_tp_fill=True)
    database = write_db(tmp_path / "old-tp.sqlite3", rows)
    pricing = simulation_policy()
    pricing = replace(
        pricing, pricing=replace(pricing.pricing, quote_latency_ms=0), delivery_latency_ms=500
    )
    request = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(database) as history:
        baseline = accounting_runner(history, pricing, request, facts).run()
        assert baseline.interruption is None
        performance = costs_for(baseline)
        report = runner_for(history, pricing, request, facts, performance).run()
        assert report.interruption is None, report.interruption
        assert report.financial_ledger == baseline.financial_ledger
        assert report.final_state == baseline.final_state
        assert report.financial_performance is not None
        value = report.financial_performance.current
        assert value.unrealized.value == 0
        assert value.rounds[0].net_realized_closed.value == value.net_cash.value
        assert report.simulation is not None
        old = next(
            o
            for o in report.simulation.orders
            if o.order.intent.reduce_only and o.cancel_confirmed_at is not None and o.fills
        )
        assert all(fill.timestamp < old.cancel_confirmed_at for fill in old.fills)  # type: ignore[operator]


def test_financial_conflict_preserves_gross_ledger_and_can_be_reviewed_on_restore(
    database: Path, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    from uuid import UUID

    from trading_engine.models.ids import OrderId
    from trading_engine.replay.ports import FillEvidence

    assert isinstance(base_fact.payload, FillEvidence)
    fill = base_fact.payload.fill
    fee = FeeEvidence(
        "contradiction",
        fill.fill_id,
        OrderId(UUID(int=999)),
        fill.timestamp,
        Decimal("1"),
        "USDC",
        FIXTURE,
    )
    at = base_fact.received_at + timedelta(seconds=1)
    performance = FinancialPerformance(
        policy(), FixtureFinancialSource((FinancialEvent(at, 0, fee),))
    )
    with SQLiteHistory(database) as history:
        request = ReplayRequest(START, END, True)
        facts = FixtureFacts((base_fact,))
        runner = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
        )
        report = runner.run()
        assert report.interruption is not None and "financial_performance" in report.interruption
        assert report.financial_ledger is not None and not report.financial_ledger.conflicts
        assert len(report.financial_ledger.entries) == 1
        assert report.financial_performance is not None and report.financial_performance.conflicts
        assert report.financial_performance.current.equity.status == "BLOCKED"
        restored = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
            snapshot=runner.snapshot,
        )
        assert restored.run() == report


def test_warmup_checkpoint_and_different_sqlite_session_rejected(database: Path) -> None:
    performance = FinancialPerformance(policy())
    request = ReplayRequest(START, END, True)
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, request, ledger=FinancialLedger(), performance=performance)
        runner.run(START - timedelta(minutes=1))
        checkpoint = runner.snapshot
        same = ReplayRunner(
            history, request, ledger=FinancialLedger(), performance=performance, snapshot=checkpoint
        )
        assert same.run() == runner.run()
    with SQLiteHistory(database) as other:
        with pytest.raises(ValueError):
            ReplayRunner(
                other,
                request,
                ledger=FinancialLedger(),
                performance=performance,
                snapshot=checkpoint,
            )


@pytest.mark.parametrize("pending_fee", [False, True])
def test_gross_fill_conflict_blocks_dependent_financial_metrics(
    database: Path, quotes: FixtureQuotes, base_fact: ReplayFact, pending_fee: bool
) -> None:
    from trading_engine.models.values import Price
    from trading_engine.replay.ports import FillEvidence

    assert isinstance(base_fact.payload, FillEvidence)
    changed = replace(
        base_fact,
        received_at=base_fact.received_at + timedelta(seconds=1),
        sequence=1,
        payload=replace(
            base_fact.payload, fill=replace(base_fact.payload.fill, price=Price(Decimal("99")))
        ),
    )
    fee = FeeEvidence(
        "pending-fee",
        base_fact.payload.fill.fill_id,
        base_fact.payload.order.order_id,
        base_fact.payload.fill.timestamp,
        Decimal("1"),
        "USDC",
        FIXTURE,
    )
    source = (
        FixtureFinancialSource((FinancialEvent(changed.received_at, 0, fee),))
        if pending_fee
        else FixtureFinancialSource()
    )
    performance = FinancialPerformance(policy(), source)
    with SQLiteHistory(database) as history:
        request = ReplayRequest(START, END, True)
        facts = FixtureFacts((base_fact, changed))
        runner = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
        )
        report = runner.run()
        assert report.interruption is not None
        assert report.financial_ledger is not None and report.financial_ledger.conflicts
        assert report.financial_performance is not None and report.financial_performance.conflicts
        assert report.financial_performance.current.equity.status == "BLOCKED"
        restored = ReplayRunner(
            history,
            request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            performance=performance,
            snapshot=runner.snapshot,
        )
        assert restored.run() == report
