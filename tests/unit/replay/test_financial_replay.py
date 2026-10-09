"""Ledger con el mismo runner/motor: delivery, shortfill, TP y fingerprints previos."""

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
from trading_engine.replay.financial_ledger import FinancialLedger, LedgerPolicy
from trading_engine.replay.financial_ledger.policy import ExecutionOrigin, ledger_context
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayReport, ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import (
    EntryControl,
    FillEvidence,
    FixtureFacts,
    FixtureQuotes,
    ReplayFact,
)
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import SimulationPolicy
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, END, START, write_db
from .test_simulated_execution import simulation_policy
from .test_simulated_replay import round_candles, setup


def accounting_runner(
    history: SQLiteHistory,
    policy: SimulationPolicy,
    request: ReplayRequest,
    facts: FixtureFacts,
    snapshot: ReplaySnapshot | None = None,
    ledger: FinancialLedger | None = None,
) -> ReplayRunner:
    quotes = ModeledQuotes(history, policy.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
    simulator = SimulatedExecution(policy, quotes)
    return ReplayRunner(
        history,
        request,
        quotes=quotes,
        simulator=simulator,
        facts=facts,
        snapshot=snapshot,
        ledger=FinancialLedger() if ledger is None else ledger,
    )


@pytest.fixture(scope="module")
def financial_round(
    tmp_path_factory: pytest.TempPathFactory,
    candles: tuple[Candle, ...],
) -> Generator[
    tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    None,
    None,
]:
    rows = round_candles(candles)
    database = write_db(tmp_path_factory.mktemp("ledger-round") / "fixture.sqlite3", rows)
    policy = simulation_policy()
    policy = replace(policy, pricing=replace(policy.pricing, quote_latency_ms=0))
    request = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(database) as history:
        runner = accounting_runner(history, policy, request, facts)
        runner.run(DUE)
        seed = runner.snapshot
        expected = runner.run()
        yield history, policy, request, facts, seed, expected, runner.snapshot


def test_complete_base_dca7_tp_closed_independent_gross_cashflow(
    financial_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
) -> None:
    _, _, _, _, _, report, snapshot = financial_round
    assert report.interruption is None and report.simulation is not None
    financial = report.financial_ledger
    assert financial is not None and financial.modeled_results
    assert report.financial_results == "PNL_BRUTO_REALIZADO_DISPONIBLE"
    assert financial.reconciliation_status == "RECONCILED"
    assert financial.open_quantity == financial.open_cost_basis_usdc == 0
    assert financial.round_ledger[0].closed_at is not None
    assert financial.round_ledger[0].average_open_cost_usdc is None
    state = report.final_state.strategy_state
    assert isinstance(state, AltBStrategyState) and len(state.round_history) == 1
    assert state.round_history[0].round_state.status(state.round_history[0].base) == "CLOSED"
    delivered = tuple(
        f
        for f in report.simulation.facts
        if isinstance(f.payload, FillEvidence) and f.received_at <= report.processed_until
    )
    assert len(financial.entries) == len(delivered)
    assert len(set(financial.processed_fill_ids)) == len(delivered)
    # Cálculo independiente sobre los fills del simulador, sin métodos del ledger ni TP teórico.
    with localcontext(ledger_context()):
        totals = []
        for side in (Side.BUY, Side.SELL):
            facts = sorted(
                (
                    f
                    for f in delivered
                    if isinstance(f.payload, FillEvidence) and f.payload.order.intent.side is side
                ),
                key=lambda f: f.payload.fill.fill_id.int
                if isinstance(f.payload, FillEvidence)
                else 0,
            )
            totals.append(
                sum(
                    (
                        f.payload.fill.quantity.value * f.payload.fill.price.value
                        for f in facts
                        if isinstance(f.payload, FillEvidence)
                    ),
                    Decimal("0"),
                )
            )
        assert financial.gross_buy_notional_usdc == totals[0]
        assert financial.gross_sell_notional_usdc == totals[1]
        assert financial.gross_realized_pnl_usdc == totals[1] - totals[0]
    assert financial.gross_realized_pnl_usdc > 0
    assert financial.provenances == (ExecutionOrigin.MODELED,)
    assert snapshot.ledger is not None and snapshot.ledger_identity == financial.identity
    data = report.as_dict()
    assert isinstance(data["financial_ledger"], dict)
    assert data["financial_ledger"]["fees"] == "NOT_AVAILABLE"


@pytest.mark.parametrize(
    "cut",
    [
        DUE,
        DUE + timedelta(minutes=2),
        DUE + timedelta(minutes=5, seconds=30),
        DUE + timedelta(minutes=6),
        DUE + timedelta(minutes=16),
    ],
)
def test_continuous_equals_fragmented_including_pending_tp(
    financial_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    cut: datetime,
) -> None:
    history, policy, request, facts, seed, expected, final = financial_round
    assert expected.interruption is None
    part = accounting_runner(history, policy, request, facts, seed)
    part.run(cut)
    checkpoint = part.snapshot
    if cut == DUE + timedelta(minutes=5, seconds=30):
        assert checkpoint.simulation is not None
        assert any(
            o.cancellation is not None and o.cancel_confirmed_at is None
            for o in checkpoint.simulation.orders
        )
    restored = accounting_runner(history, policy, request, facts, checkpoint)
    assert restored.run() == expected and restored.snapshot == final


@pytest.mark.parametrize("delivery_ms", [0, 500])
@pytest.mark.parametrize("volume", ["0", "1", "1000"])
def test_base_shortfill_and_delivery_availability(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    delivery_ms: int,
    volume: str,
) -> None:
    rows = round_candles(candles)
    at = DUE + timedelta(minutes=2)
    rows = tuple(
        replace(c, volume=Decimal(volume), taker_buy_base_volume=Decimal(volume) / Decimal("2"))
        if c.close_time == at
        else c
        for c in rows
    )
    database = write_db(tmp_path / "shortfill.sqlite3", rows)
    policy = simulation_policy()
    policy = replace(
        policy, pricing=replace(policy.pricing, quote_latency_ms=0), delivery_latency_ms=delivery_ms
    )
    request = ReplayRequest(START, DUE + timedelta(minutes=5), True)
    with SQLiteHistory(database) as history:
        runner = accounting_runner(history, policy, request, FixtureFacts())
        first = runner.run(at)
        assert first.interruption is None and first.financial_ledger is not None
        if delivery_ms and volume != "0":
            assert first.simulation is not None and first.simulation.orders[0].fills
            assert first.financial_ledger.entries == ()
            before = runner.run(at + timedelta(milliseconds=499))
            assert (
                before.financial_ledger is not None and before.financial_ledger.open_quantity == 0
            )
        expected = runner.run()
        assert expected.interruption is None and expected.financial_ledger is not None
        financial = expected.financial_ledger
        strategy = expected.final_state.strategy_state
        assert isinstance(strategy, AltBStrategyState) and strategy.base is not None
        if volume == "1":
            assert (
                strategy.base.p0 is None
                and strategy.base.block_reason == "base_shortfill_reconciliation"
            )
            assert strategy.round_state is None
            assert financial.open_quantity == Decimal("0.05") and financial.open_cost_basis_usdc > 0
            assert financial.gross_realized_pnl_usdc == 0 and len(financial.entries) == 1
        elif volume == "0":
            assert financial.entries == () and financial.open_quantity == 0
        else:
            assert strategy.round_state is not None and len(strategy.round_state.dcas) == 7
        # Reanudar desde delivery pendiente y resolución terminal MARKET pendiente.
        part = accounting_runner(history, policy, request, FixtureFacts())
        for cut in (
            at,
            at + timedelta(milliseconds=50),
            at + timedelta(milliseconds=100),
            at + timedelta(milliseconds=500),
        ):
            part.run(cut)
            part = accounting_runner(history, policy, request, FixtureFacts(), part.snapshot)
        assert part.run() == expected and part.snapshot == runner.snapshot


def test_old_tp_partial_cancel_replacement_and_buy_after_sale(
    tmp_path: Path,
    candles: tuple[Candle, ...],
) -> None:
    rows = round_candles(candles, old_tp_fill=True)
    database = write_db(tmp_path / "old-tp.sqlite3", rows)
    policy = simulation_policy()
    policy = replace(policy, pricing=replace(policy.pricing, quote_latency_ms=0))
    request = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(database) as history:
        runner = accounting_runner(history, policy, request, facts)
        first = runner.run(DUE + timedelta(minutes=5))
        assert first.interruption is None and first.financial_ledger is not None
        assert first.simulation is not None
        old = next(o for o in first.simulation.orders if o.order.intent.reduce_only)
        assert old.fills and old.cancel_confirmed_at is None
        previous = first.financial_ledger.gross_sell_notional_usdc
        second = runner.run(DUE + timedelta(minutes=6))
        assert second.interruption is None and second.financial_ledger is not None
        assert second.financial_ledger.gross_sell_notional_usdc == previous
        assert second.simulation is not None
        tps = [o for o in second.simulation.orders if o.order.intent.reduce_only]
        assert len(tps) == 2 and tps[0].cancel_confirmed_at is not None
        checkpoint = runner.snapshot
        expected = runner.run()
        assert expected.interruption is None and expected.financial_ledger is not None
        assert len(expected.financial_ledger.processed_fill_ids) == len(
            set(expected.financial_ledger.processed_fill_ids)
        )
        restored = accounting_runner(history, policy, request, facts, checkpoint)
        assert restored.run() == expected and restored.snapshot == runner.snapshot
        entries = expected.financial_ledger.entries
        assert any(
            a.side is Side.SELL and b.side is Side.BUY
            for a, b in zip(entries, entries[1:], strict=False)
        )


@pytest.mark.parametrize("damage", ["cost", "lost_state", "identity", "other_policy", "disabled"])
def test_restore_rejects_modified_accounting_or_policy(
    financial_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    damage: str,
) -> None:
    history, policy, request, facts, _, _, snapshot = financial_round
    assert snapshot.ledger is not None
    ledger = FinancialLedger()
    if damage == "cost":
        r = replace(snapshot.ledger.round_ledger[0], open_cost_basis_usdc=Decimal("1"))
        snapshot = replace(snapshot, ledger=replace(snapshot.ledger, round_ledger=(r,)))
    elif damage == "lost_state":
        snapshot = replace(snapshot, ledger=None)
    elif damage == "identity":
        snapshot = replace(snapshot, ledger_identity="other")
    elif damage == "other_policy":
        ledger = FinancialLedger(LedgerPolicy(allowed_origins=(ExecutionOrigin.MODELED,)))
    snapshot = replace(snapshot, integrity_digest=snapshot.calculate_integrity())
    with pytest.raises(ValueError):
        if damage == "disabled":
            setup(history, policy, request, facts, snapshot)
        else:
            accounting_runner(history, policy, request, facts, snapshot, ledger)


@pytest.mark.parametrize("mode", ["no_quotes", "fixture_quotes", "modeled_simulator"])
def test_disabled_ledger_fingerprints_frozen_from_approved_main(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    quotes: FixtureQuotes,
    mode: str,
) -> None:
    baseline = json.loads(
        (Path(__file__).parent / "fixtures/financial_ledger_disabled_baselines.json").read_text()
    )[mode]
    database = write_db(tmp_path / "compat.sqlite3", round_candles(candles))
    request = ReplayRequest(START, END, True)
    with SQLiteHistory(database) as history:
        if mode == "modeled_simulator":
            p = simulation_policy()
            p = replace(p, pricing=replace(p.pricing, quote_latency_ms=0))
            runner = setup(history, p, request, FixtureFacts())
        else:
            runner = ReplayRunner(history, request, quotes=None if mode == "no_quotes" else quotes)
        report = runner.run()
        assert report.financial_ledger is None and runner.snapshot.ledger is None
        assert report.financial_results == "FINANCIAL_RESULTS_NOT_AVAILABLE"
        assert report.event_digest == baseline["event_digest"]
        assert runner.snapshot.integrity_digest == baseline["integrity_digest"]
        assert sha256(encoded(report)).hexdigest() == baseline["report_digest"]
        rendered = json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")).encode()
        assert sha256(rendered).hexdigest() == baseline["as_dict_digest"]
        assert "financial_ledger" not in report.as_dict()


def test_gaps_keep_confirmed_economic_exposure(
    tmp_path: Path,
    candles: tuple[Candle, ...],
) -> None:
    rows = round_candles(candles)
    rows = tuple(c for c in rows if c.close_time != DUE + timedelta(minutes=5))
    database = write_db(tmp_path / "gap.sqlite3", rows)
    p = simulation_policy()
    p = replace(p, pricing=replace(p.pricing, quote_latency_ms=0))
    request = ReplayRequest(START, DUE + timedelta(minutes=5), True)
    with SQLiteHistory(database) as history:
        runner = accounting_runner(history, p, request, FixtureFacts())
        before = runner.run(DUE + timedelta(minutes=4))
        after = runner.run()
        assert before.interruption is None and after.interruption is None
        assert before.financial_ledger is not None and after.financial_ledger is not None
        assert before.financial_ledger == after.financial_ledger
        assert after.financial_ledger.open_quantity > 0 and after.stats.invalid_periods


def test_no_quotes_active_empty_ledger(tmp_path: Path, candles: tuple[Candle, ...]) -> None:
    database = write_db(tmp_path / "noquotes.sqlite3", candles)
    with SQLiteHistory(database) as history:
        report = ReplayRunner(
            history, ReplayRequest(START, END, True), ledger=FinancialLedger()
        ).run()
        assert report.interruption is None and report.financial_ledger is not None
        assert report.financial_ledger.entries == ()
        assert report.financial_results == "PNL_BRUTO_REALIZADO_DISPONIBLE"
        assert report.financial_ledger.gross_realized_pnl_usdc == 0


def test_offline_ledger_no_network_or_live_dependency(
    financial_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import urllib.request

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Ledger offline no admite red")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    history, p, request, facts, seed, expected, _ = financial_round
    assert accounting_runner(history, p, request, facts, seed).run() == expected
    root = Path(__file__).resolve().parents[3] / "src/trading_engine/replay/financial_ledger"
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


def test_fixture_quotes_orders_and_duplicate_fills_count_once(
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    base_fact: ReplayFact,
) -> None:
    assert isinstance(base_fact.payload, FillEvidence)
    facts = FixtureFacts(
        (
            replace(base_fact, payload=base_fact.payload.order, sequence=1),
            replace(base_fact, sequence=2),
            replace(base_fact, received_at=DUE + timedelta(seconds=2), sequence=3),
            replace(
                base_fact,
                received_at=DUE + timedelta(seconds=3),
                payload=base_fact.payload.order,
                sequence=4,
            ),
        )
    )
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history, replay_request, quotes=quotes, facts=facts, ledger=FinancialLedger()
        )
        before = runner.run(DUE)
        assert before.financial_ledger is not None and not before.financial_ledger.entries
        first = runner.run(DUE + timedelta(seconds=1))
        last = runner.run()
        assert first.interruption is None and last.interruption is None
        assert first.financial_ledger == last.financial_ledger
        assert last.financial_ledger is not None and len(last.financial_ledger.entries) == 1
        assert last.financial_ledger.provenances == (ExecutionOrigin.FIXTURE,)
        assert not last.financial_ledger.modeled_results
        assert last.financial_ledger.open_quantity == base_fact.payload.fill.quantity.value


@pytest.mark.parametrize("cause", ["late", "oversell"])
def test_runner_blocks_unresolvable_fixture_accounting(
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    base_fact: ReplayFact,
    cause: str,
) -> None:
    from trading_engine.models.enums import OrderStatus

    from .conftest import fill_for, order_for

    with SQLiteHistory(database) as history:
        seed = ReplayRunner(
            history, replay_request, quotes=quotes, facts=FixtureFacts((base_fact,))
        )
        seed.run(DUE + timedelta(seconds=1))
        strategy = seed.snapshot.coordinator.strategy_state
        assert isinstance(strategy, AltBStrategyState) and strategy.round_state is not None
        if cause == "oversell":
            intent = strategy.round_state.take_profits[0].intent
            order = order_for(
                intent, 201, DUE + timedelta(seconds=3), OrderStatus.FILLED, intent.quantity.value
            )
            with localcontext(ledger_context()):
                quantity = intent.quantity.value + Decimal("1")
            from trading_engine.models.values import Quantity

            fill = replace(fill_for(order, 2001, "101"), quantity=Quantity(quantity))
            extra: tuple[ReplayFact, ...] = (
                ReplayFact(order.updated_at, FillEvidence(fill, order), 1),
            )
        else:
            intent = strategy.round_state.dcas[0].intent
            order = order_for(
                intent,
                301,
                DUE + timedelta(seconds=2),
                OrderStatus.PARTIALLY_FILLED,
                Decimal("0.25"),
            )
            first = ReplayFact(
                order.updated_at, FillEvidence(fill_for(order, 2001, "98"), order), 1
            )
            older = replace(
                order, updated_at=DUE + timedelta(milliseconds=1500), filled_quantity=Decimal("0.5")
            )
            fill = replace(fill_for(older, 2002, "90"), quantity=fill_for(order, 2001).quantity)
            extra = (first, ReplayFact(DUE + timedelta(seconds=3), FillEvidence(fill, older), 2))
        facts = FixtureFacts((base_fact, *extra))
        runner = ReplayRunner(
            history, replay_request, quotes=quotes, facts=facts, ledger=FinancialLedger()
        )
        report = runner.run()
        assert report.interruption is not None and "financial_ledger:" in report.interruption
        assert report.financial_results == "PNL_BRUTO_REALIZADO_BLOQUEADO"
        financial = report.financial_ledger
        assert financial is not None and financial.conflicts
        assert financial.reconciliation_status == "RECONCILIATION_REQUIRED"
        assert financial.open_quantity > 0 and financial.gross_realized_pnl_usdc == 0
        assert all(e.side is Side.BUY for e in financial.entries)
        assert len(financial.entries) == (1 if cause == "oversell" else 2)
        # El bloqueo es terminal; restaurarlo no permite saltar el hecho rechazado.
        restored = ReplayRunner(
            history,
            replay_request,
            quotes=quotes,
            facts=facts,
            ledger=FinancialLedger(),
            snapshot=runner.snapshot,
        )
        assert restored.run() == report
