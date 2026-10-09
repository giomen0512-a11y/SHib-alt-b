"""Rondas sintéticas con el mismo AltBEngine: conciliación y reanudación exactas."""

import ast
from collections.abc import Generator
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from trading_engine.models.enums import OrderStatus
from trading_engine.models.market import Candle
from trading_engine.models.values import Price
from trading_engine.replay.execution_model.config import load_profiles
from trading_engine.replay.execution_model.contracts import price_context
from trading_engine.replay.execution_model.policy import Scenario
from trading_engine.replay.execution_model.quotes import ModeledQuotes
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayReport, ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import EntryControl, FixtureFacts, NoQuotes, ReplayFact
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution.core import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import QueuePolicy, SimulationPolicy
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, START, T, write_db
from .test_execution_policy import FIXTURE_PATH
from .test_simulated_execution import simulation_policy


def round_candles(candles: tuple[Candle, ...], *, old_tp_fill: bool = False) -> tuple[Candle, ...]:
    result = list(c for c in candles if c.close_time <= DUE + timedelta(minutes=2))
    # La Base completa debe tener capacidad suficiente en su única oportunidad.
    result[-1] = replace(result[-1], volume=Decimal("1000"), taker_buy_base_volume=Decimal("500"))
    first = result[-1]
    # OHLC planos: no se fabrica recorrido. DCA1 y TP requieren varios fills.
    prices = [
        "100",
        "98",
        "98",
        "98",
        "98",
        "96",
        "94",
        "92",
        "89",
        "86",
        "83",
        "83",
        "83",
        "102",
        "102",
        "102",
        "102",
        "102",
        "102",
    ]
    if old_tp_fill:
        prices[2] = "102"  # TP viejo activo antes de confirmar su cancelación
    for index, value in enumerate(prices):
        at = first.close_time + timedelta(minutes=index + 1)
        volume = Decimal("10") if index in {1, 2, 3, 4, 13} else Decimal("1000")
        price = Price(Decimal(value))
        result.append(
            replace(
                first,
                open_time=at - timedelta(minutes=1),
                close_time=at,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
                taker_buy_base_volume=volume / Decimal("2"),
            )
        )
    return tuple(result)


def setup(
    history: SQLiteHistory,
    policy: SimulationPolicy,
    request: ReplayRequest,
    facts: FixtureFacts,
    snapshot: ReplaySnapshot | None = None,
) -> ReplayRunner:
    quotes = ModeledQuotes(history, policy.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
    simulator = SimulatedExecution(policy, quotes)
    return ReplayRunner(
        history, request, quotes=quotes, simulator=simulator, facts=facts, snapshot=snapshot
    )


@pytest.fixture(scope="module")
def complete_round(
    tmp_path_factory: pytest.TempPathFactory, candles: tuple[Candle, ...]
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
    path = write_db(tmp_path_factory.mktemp("simulated-round") / "fixture.sqlite3", rows)
    policy = simulation_policy()
    policy = replace(policy, pricing=replace(policy.pricing, quote_latency_ms=0))
    request = ReplayRequest(START, rows[-1].close_time, allow_entries=True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(path) as history:
        runner = setup(history, policy, request, facts)
        runner.run(DUE)
        seed = runner.snapshot
        expected = runner.run()
        yield history, policy, request, facts, seed, expected, runner.snapshot


def test_complete_base_seven_dcas_dynamic_tp_and_closed_round(
    complete_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
) -> None:
    _, _, _, _, seed, report, _ = complete_round
    assert report.interruption is None
    assert (
        seed.simulation is not None
        and seed.simulation.orders[0].order.status is OrderStatus.PENDING
    )
    assert not seed.simulation.orders[
        0
    ].fills  # Base emitida exactamente T+120, sin ejecución automática
    strategy = report.final_state.strategy_state
    assert isinstance(strategy, AltBStrategyState)
    assert strategy.base is None and strategy.round_state is None
    assert len(strategy.round_history) == 1
    record = strategy.round_history[0]
    assert len(record.base.fills) == 1
    assert record.round_state.status(record.base) == "CLOSED"
    assert record.round_state.quantity_open(record.base) == 0
    assert all(
        leg.order is not None and leg.order.status is OrderStatus.FILLED
        for leg in record.round_state.dcas
    )
    assert len(record.round_state.dcas[0].fills) > 1
    assert any(len(leg.fills) > 1 for leg in record.round_state.take_profits)
    assert len(record.round_state.take_profits) > 1
    assert record.round_state.tp_target(record.base).value < record.base.p0.value  # type: ignore[union-attr]
    assert report.financial_results == "FINANCIAL_RESULTS_NOT_AVAILABLE"
    assert report.execution_metrics is not None and report.simulation is not None
    assert report.execution_metrics.fills_generated == sum(
        len(o.fills) for o in report.simulation.orders
    )
    assert all(f.provenance == "MODELED_EXECUTION" for f in report.simulation.facts)
    assert all(v.used <= v.cap for v in report.simulation.liquidity)
    assert report.execution_metrics.cancellations_confirmed >= 1
    assert report.execution_metrics.reconciliation_conflicts == 0


@pytest.mark.parametrize(
    "cut",
    [
        DUE,
        DUE + timedelta(milliseconds=50),
        DUE + timedelta(milliseconds=100),
        DUE + timedelta(minutes=5, seconds=30),
        DUE + timedelta(minutes=6),
        DUE + timedelta(minutes=16),
    ],
)
def test_continuous_fragmented_exact_with_pending_cancellation(
    complete_round: tuple[
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
    history, policy, request, facts, seed, expected, final = complete_round
    part = setup(history, policy, request, facts, seed)
    part.run(cut)
    checkpoint = part.snapshot
    if cut == DUE + timedelta(minutes=5, seconds=30):
        assert checkpoint.simulation is not None
        assert any(
            o.cancellation is not None and o.cancel_confirmed_at is None
            for o in checkpoint.simulation.orders
        )
    resumed = setup(history, policy, request, facts, checkpoint)
    assert resumed.run() == expected
    assert resumed.snapshot == final
    assert resumed.run() == expected  # retry sin nuevas órdenes/fills


@pytest.mark.parametrize(
    "changed",
    [
        "scenario",
        "acceptance",
        "execution",
        "cancellation",
        "delivery",
        "queue",
        "dataset",
        "version",
        "disabled",
    ],
)
def test_incompatible_resume_rejected(
    complete_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
    changed: str,
) -> None:
    history, policy, request, facts, seed, _, _ = complete_round
    if changed == "version":
        with pytest.raises(ValueError, match="Versión"):
            replace(policy, rules_version="other-rules")
        return
    if changed == "disabled":
        quotes = ModeledQuotes(history, policy.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
        with pytest.raises(ValueError, match="Reanudación"):
            ReplayRunner(history, request, quotes=quotes, facts=facts, snapshot=seed)
        return
    updates = {
        "scenario": replace(policy, pricing=load_profiles(FIXTURE_PATH).select(Scenario.ADVERSE)),
        "acceptance": replace(policy, acceptance_latency_ms=101),
        "execution": replace(policy, execution_delay_ms=101),
        "cancellation": replace(policy, cancellation_latency_ms=120001),
        "delivery": replace(policy, delivery_latency_ms=1),
        "queue": replace(policy, queue=QueuePolicy.WITHHOLD),
    }
    selected = updates.get(changed, policy)
    quotes = ModeledQuotes(
        history,
        selected.pricing,
        "OTHER" if changed == "dataset" else "SYNTHETIC_ROUND_FIXTURE_ONLY",
    )
    with pytest.raises(ValueError, match="Reanudación"):
        ReplayRunner(
            history,
            request,
            quotes=quotes,
            simulator=SimulatedExecution(selected, quotes),
            facts=facts,
            snapshot=seed,
        )


def test_snapshot_missing_execution_state_rejected(
    complete_round: tuple[
        SQLiteHistory,
        SimulationPolicy,
        ReplayRequest,
        FixtureFacts,
        ReplaySnapshot,
        ReplayReport,
        ReplaySnapshot,
    ],
) -> None:
    history, policy, request, facts, seed, _, _ = complete_round
    changed = replace(seed, simulation=None)
    changed = replace(changed, integrity_digest=changed.calculate_integrity())
    with pytest.raises(ValueError, match="perdió"):
        setup(history, policy, request, facts, changed)


def test_old_tp_partial_fill_before_cancel_confirmation_and_single_replacement(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    rows = round_candles(candles, old_tp_fill=True)
    path = write_db(tmp_path / "cancel-race.sqlite3", rows)
    p = simulation_policy()
    p = replace(p, pricing=replace(p.pricing, quote_latency_ms=0))
    request = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(path) as history:
        runner = setup(history, p, request, facts)
        report = runner.run(DUE + timedelta(minutes=5))
        assert report.interruption is None and report.simulation is not None
        old = next(o for o in report.simulation.orders if o.order.intent.reduce_only)
        assert old.cancellation is not None and old.cancel_confirmed_at is None
        assert old.order.status is OrderStatus.PARTIALLY_FILLED
        assert old.fills[0].timestamp < old.cancel_confirm_at  # type: ignore[operator]
        assert sum(o.order.intent.reduce_only for o in report.simulation.orders) == 1
        report = runner.run(DUE + timedelta(minutes=6))
        assert report.interruption is None and report.simulation is not None
        old = (
            report.simulation.orders[report.simulation.orders.index(old)]
            if old in report.simulation.orders
            else next(
                o for o in report.simulation.orders if o.order.intent_id == old.order.intent_id
            )
        )
        assert old.order.status is OrderStatus.CANCELED
        assert old.order.filled_quantity == Decimal("0.5")
        tps = [o for o in report.simulation.orders if o.order.intent.reduce_only]
        assert len(tps) == 2
        assert tps[1].emitted_at >= old.cancel_confirmed_at  # type: ignore[operator]
        strategy = report.final_state.strategy_state
        assert (
            isinstance(strategy, AltBStrategyState)
            and strategy.base is not None
            and strategy.round_state is not None
        )
        # La confirmación precede al cierre del mismo instante: un DCA posterior
        # puede volver a cambiar el objetivo, pero nunca se solapan dos TP.
        assert tps[-1].order.intent.quantity.value == strategy.base.executed_quantity
        assert tps[-1].cancellation is not None
        with localcontext(price_context()):
            assert strategy.round_state.quantity_open(strategy.base) == (
                strategy.base.executed_quantity + Decimal("0.5")
            )


def test_simulator_no_network_no_live_dependency(
    complete_round: tuple[
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
        raise AssertionError("Simulador offline no admite red")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    history, policy, request, facts, seed, expected, _ = complete_round
    assert setup(history, policy, request, facts, seed).run() == expected
    root = Path(__file__).resolve().parents[3] / "src/trading_engine/replay/simulated_execution"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    (
                        "trading_engine.strategy",
                        "trading_engine.execution",
                        "trading_engine.binance.public_data",
                        "socket",
                        "urllib",
                        "http",
                        "requests",
                        "random",
                    )
                )
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id != "uuid4"
                elif isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {"now", "submit_order", "poll", "sleep"}


@pytest.mark.parametrize("scenario", list(Scenario))
def test_profiles_run_same_engine_and_delivery_resume(database: Path, scenario: Scenario) -> None:
    policy = replace(simulation_policy(scenario), delivery_latency_ms=500)
    request = ReplayRequest(START, T + timedelta(minutes=5), True)
    facts = FixtureFacts()
    with SQLiteHistory(database) as history:
        continuous = setup(history, policy, request, facts)
        continuous.run(DUE)
        seed = continuous.snapshot
        expected = continuous.run()
        part = setup(history, policy, request, facts, seed)
        part.run(DUE + timedelta(milliseconds=300))
        assert part.snapshot.simulation is not None
        assert part.snapshot.simulation.deliveries  # aceptación ocurrida, evidencia aún no recibida
        resumed = setup(history, policy, request, facts, part.snapshot)
        assert resumed.run() == expected
        assert resumed.snapshot == continuous.snapshot
        assert expected.execution_metrics is not None
        assert expected.execution_metrics.scenario == scenario.value
        assert expected.financial_results == "FINANCIAL_RESULTS_NOT_AVAILABLE"


def test_external_fill_cannot_corrupt_modeled_execution(
    database: Path, base_fact: ReplayFact
) -> None:
    with SQLiteHistory(database) as history:
        report = setup(
            history,
            simulation_policy(),
            ReplayRequest(START, T + timedelta(minutes=5), True),
            FixtureFacts((base_fact,)),
        ).run()
        assert report.interruption is not None and "No mezclar" in report.interruption
        assert report.execution_metrics is not None
        assert report.execution_metrics.reconciliation_conflicts == 1
        assert report.execution_metrics.fills_generated == 0


def test_noquotes_remains_off_and_no_execution(database: Path) -> None:
    with SQLiteHistory(database) as history:
        report = ReplayRunner(
            history, ReplayRequest(START, T + timedelta(minutes=5), True), quotes=NoQuotes()
        ).run()
        assert report.simulation is None and report.execution_metrics is None
        assert (
            report.event_digest
            == "1de51441efde90d5059802f62aac9fb21926a16be4dd23b1327109f352d96746"
        )
