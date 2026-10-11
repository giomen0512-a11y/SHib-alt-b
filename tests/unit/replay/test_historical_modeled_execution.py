"""5D2: mercado sintético, Quotes MODELED y matching aprobado, exclusivamente offline."""

import json
import socket
import sqlite3
from collections.abc import Generator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from trading_engine.binance.klines import LAUNCH
from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.market import Candle
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import canonical, encoded
from trading_engine.replay.execution_model.policy import Scenario
from trading_engine.replay.execution_model.quotes import ModeledQuoteEvidence, ModeledQuotes
from trading_engine.replay.historical_validation.replay_adapter import PreparedTradeHistory
from trading_engine.replay.historical_validation.verification import VerificationMode
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayReport, ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import (
    CandleHistory,
    EntryControl,
    FixtureFacts,
    FixtureQuotes,
    NoQuotes,
    ReplayFact,
)
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution.core import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import SimulationPolicy
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, END, START, SYMBOL, T, write_db
from .test_historical_fast_query import registered, session
from .test_historical_replay_adapter import small_registry, trade_registration
from .test_simulated_execution import drain, intent, opened, simulation_policy
from .test_simulated_replay import round_candles, setup

MINUTE = timedelta(minutes=1)
STRICT = VerificationMode.STRICT_FULL
SEGMENTED = VerificationMode.SEALED_SEGMENT_SESSION
BASELINES = json.loads(
    (Path(__file__).parent / "fixtures/historical_modeled_baselines.json").read_text()
)


@contextmanager
def prepared(
    path: Path, ids: tuple[str, ...], request: ReplayRequest, mode: VerificationMode
) -> Iterator[PreparedTradeHistory]:
    with ExitStack() as stack:
        active = stack.enter_context(session(path, ids)) if mode is SEGMENTED else None
        yield stack.enter_context(
            PreparedTradeHistory(path, ids, request, mode=mode, session=active)
        )


def runner_for(
    history: PreparedTradeHistory,
    policy: SimulationPolicy,
    request: ReplayRequest,
    facts: FixtureFacts | None = None,
    snapshot: ReplaySnapshot | None = None,
) -> ReplayRunner:
    source = ModeledQuotes(history, policy.pricing, history.evidence.identity)
    return ReplayRunner(
        history,
        request,
        quotes=source,
        simulator=SimulatedExecution(policy, source),
        facts=facts,
        snapshot=snapshot,
    )


def fingerprint(runner: ReplayRunner) -> dict[str, object]:
    report = runner.run()
    return {
        "event_digest": report.event_digest,
        "report_sha256": sha256(encoded(report)).hexdigest(),
        "emissions_sha256": sha256(encoded(runner.snapshot.emissions)).hexdigest(),
        "snapshot_integrity": runner.snapshot.integrity_digest,
        "quote_identity": runner.quotes.identity,
        "simulation_identity": None if runner.simulator is None else runner.simulator.identity,
    }


@pytest.mark.parametrize("inputs", ["no_quotes", "fixture_quotes", "fixture_facts"])
def test_frozen_main_legacy_ports(
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    inputs: str,
) -> None:
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history,
            replay_request,
            quotes=NoQuotes() if inputs == "no_quotes" else quotes,
            facts=facts if inputs == "fixture_facts" else FixtureFacts(),
        )
        assert fingerprint(runner) == BASELINES[inputs]


@pytest.mark.parametrize("scenario", list(Scenario))
def test_frozen_main_sqlite_modeled(
    database: Path, replay_request: ReplayRequest, scenario: Scenario
) -> None:
    with SQLiteHistory(database) as history:
        source = ModeledQuotes(
            history, simulation_policy(scenario).pricing, "SYNTHETIC_FIXTURE_MARKET"
        )
        assert (
            fingerprint(ReplayRunner(history, replay_request, quotes=source))
            == BASELINES["modeled_" + scenario.value]
        )


@pytest.mark.parametrize("scenario", list(Scenario))
def test_frozen_main_sqlite_simulator(
    tmp_path: Path, candles: tuple[Candle, ...], scenario: Scenario
) -> None:
    rows = round_candles(candles)
    path = write_db(tmp_path / "round.sqlite3", rows)
    policy = simulation_policy(scenario)
    policy = replace(policy, pricing=replace(policy.pricing, quote_latency_ms=0))
    request = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(path) as history:
        assert (
            fingerprint(setup(history, policy, request, facts))
            == BASELINES["simulated_" + scenario.value]
        )


@dataclass
class CompatibleHistory:
    """Puerto estructural independiente; no hereda SQLite ni copia su base."""

    candle: Candle
    session: object

    def conflicts(self, symbol: Symbol) -> tuple[str, ...]:
        return ()

    def read(self, symbol: Symbol, start: datetime, end: datetime) -> Generator[Candle, None, None]:
        if self.candle.symbol == symbol and start <= self.candle.open_time < end:
            yield self.candle


def test_candlehistory_is_structural(candles: tuple[Candle, ...]) -> None:
    history = CompatibleHistory(candles[0], object())
    assert isinstance(history, CandleHistory)
    source = ModeledQuotes(history, simulation_policy(Scenario.FAVORABLE).pricing, "synthetic-port")
    assert source.at(SYMBOL, candles[0].close_time) is not None
    with pytest.raises(TypeError, match="CandleHistory"):
        ModeledQuotes(object(), source.policy, "wrong")  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", [STRICT, SEGMENTED])
def test_identity_and_same_session_required(tmp_path: Path, mode: VerificationMode) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with prepared(path, ids, request, mode) as first:
        with prepared(path, ids, request, mode) as second:
            source = ModeledQuotes(first, simulation_policy().pricing, first.evidence.identity)
            assert source.dataset_identity == first.evidence.identity
            assert source.history_session is first.session
            for wrong in ("arbitrary", ids[0], second.evidence.identity + "-wrong"):
                with pytest.raises(ValueError, match="DATASET_MISMATCH"):
                    ModeledQuotes(first, source.policy, wrong)
            with pytest.raises(ValueError, match="misma sesión"):
                ReplayRunner(second, request, quotes=source)
            runner = ReplayRunner(first, request, quotes=source)
            runner.run(LAUNCH + MINUTE)
            other = ModeledQuotes(second, source.policy, second.evidence.identity)
            with pytest.raises(ValueError, match="Reanudación"):
                ReplayRunner(second, request, quotes=other, snapshot=runner.snapshot)
            original = first.evidence
            first.evidence = replace(original, identity="changed-evidence")
            with pytest.raises(ValueError, match="DATASET_MISMATCH"):
                source.estimate(SYMBOL, LAUNCH + MINUTE)
            first.evidence = original


@pytest.mark.parametrize("mode", [STRICT, SEGMENTED])
@pytest.mark.parametrize(
    "milliseconds,expected_close,reason",
    [
        (249, -1, "stale_reference"),
        (250, 0, "modeled_quote_available"),
        (500, 0, "modeled_quote_available"),
        (501, 0, "stale_reference"),
    ],
)
def test_latency_freshness_boundaries(
    tmp_path: Path,
    mode: VerificationMode,
    milliseconds: int,
    expected_close: int,
    reason: str,
) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    policy = replace(simulation_policy().pricing, freshness_limit_ms=500)
    with prepared(path, ids, request, mode) as history:
        source = ModeledQuotes(history, policy, history.evidence.identity)
        at = LAUNCH + 2 * MINUTE + timedelta(milliseconds=milliseconds)
        result = source.estimate(SYMBOL, at)
        assert result.reason == reason
        if result.evidence is not None:
            evidence = result.evidence
            assert evidence.reference_candle.close_time == LAUNCH + (2 + expected_close) * MINUTE
            assert evidence.quote.timestamp == at
            assert evidence.available_at == LAUNCH + 2 * MINUTE + timedelta(milliseconds=250)
            assert evidence.expires_at == LAUNCH + 2 * MINUTE + timedelta(milliseconds=500)
            assert evidence.dataset_identity == history.evidence.identity
        else:
            assert reason == "stale_reference"


@pytest.mark.parametrize("mode", [STRICT, SEGMENTED])
@pytest.mark.parametrize("missing", [DUE - MINUTE, DUE])
def test_economic_gaps_are_unavailable_without_integrity_failure(
    tmp_path: Path, candles: tuple[Candle, ...], mode: VerificationMode, missing: datetime
) -> None:
    rows = tuple(c for c in candles if c.close_time != missing)
    path, ids = registered(tmp_path, trade_registration(tmp_path, rows))
    request = ReplayRequest(START, END, True)
    with prepared(path, ids, request, mode) as history:
        source = ModeledQuotes(history, simulation_policy().pricing, history.evidence.identity)
        result = source.estimate(SYMBOL, DUE)
        assert result.evidence is None and "gap" in result.reason
        assert history.session is source.history_session
        report = runner_for(history, simulation_policy(), request).run()
        assert report.interruption is None
        assert report.order_intents == 0
        assert report.simulation is not None and not report.simulation.orders
        assert not report.simulation.facts


@pytest.mark.parametrize("mode", [STRICT, SEGMENTED])
def test_future_and_contract_reads_are_blocked(tmp_path: Path, mode: VerificationMode) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with prepared(path, ids, request, mode) as history:
        source = ModeledQuotes(history, simulation_policy().pricing, history.evidence.identity)
        with pytest.raises(ValueError, match="CUTOFF"):
            source.estimate(SYMBOL, request.end + timedelta(microseconds=1))
        with pytest.raises(ValueError, match="CONTRACT_MISMATCH"):
            source.estimate(Symbol("SHIBUSDC"), LAUNCH + MINUTE)
        with pytest.raises(ValueError, match="OUTSIDE_PREPARED_REQUEST"):
            source.estimate(
                SYMBOL,
                request.warmup_start(
                    runner_for(history, simulation_policy(), request).snapshot.config
                ),
            )


@pytest.mark.parametrize("mode", [STRICT, SEGMENTED])
def test_exact_closed_prefix_and_no_future_price(
    tmp_path: Path, mode: VerificationMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with prepared(path, ids, request, mode) as history:
        policy = simulation_policy().pricing
        calls: list[tuple[datetime, datetime]] = []
        original = history.read

        def guarded(
            symbol: Symbol, start: datetime, end: datetime
        ) -> Generator[Candle, None, None]:
            calls.append((start, end))
            assert end <= LAUNCH + 2 * MINUTE
            yield from original(symbol, start, end)

        monkeypatch.setattr(history, "read", guarded)
        source = ModeledQuotes(history, policy, history.evidence.identity)
        at = LAUNCH + 2 * MINUTE
        evidence = source.at(SYMBOL, at)
        assert isinstance(evidence, ModeledQuoteEvidence)
        assert evidence.reference_candle.close_time == at - MINUTE
        assert calls == [(LAUNCH, at)]
        assert history.evidence.bindings[0].registration.declaration.acquisition_time > at


@pytest.mark.parametrize("fault", ["closed", "expired", "invalid", "source", "session_closed"])
def test_trust_errors_block_quotes_and_replay(tmp_path: Path, fault: str) -> None:
    path, ids, request, registration = small_registry(tmp_path)
    with session(path, ids) as active:
        with PreparedTradeHistory(path, ids, request, mode=SEGMENTED, session=active) as history:
            source = ModeledQuotes(history, simulation_policy().pricing, history.evidence.identity)
            runner = runner_for(history, simulation_policy(), request)
            expected = {
                "closed": "PREPARED_HISTORY_NOT_AVAILABLE",
                "expired": "SESSION_EXPIRED",
                "invalid": "VERIFICATION_NOT_AVAILABLE",
                "source": "SOURCE_CHANGED",
                "session_closed": "VERIFICATION_NOT_AVAILABLE",
            }[fault]
            if fault == "closed":
                history.close()
            elif fault == "expired":
                active._started -= active.policy.max_seconds + 1
            elif fault == "invalid":
                active._invalid = True
            elif fault == "source":
                p = Path(registration.original_file)
                p.write_text(p.read_text() + "\n")
            else:
                active.close()
            with pytest.raises(ValueError, match=expected):
                source.estimate(SYMBOL, LAUNCH + 2 * MINUTE)
            report = runner.run()
            # Expirar o cambiar la fuente invalida permanentemente la sesión aprobada.
            subsequent = (
                "VERIFICATION_NOT_AVAILABLE" if fault in {"expired", "source"} else expected
            )
            assert report.interruption is not None and subsequent in report.interruption
            assert not any(e.kind == "missing_quote" for e in runner.snapshot.emissions)
            assert report.simulation is not None and not report.simulation.facts
            with pytest.raises(ValueError):
                runner_for(history, simulation_policy(), request, snapshot=runner.snapshot)


@pytest.mark.parametrize("mode", [STRICT, SEGMENTED])
@pytest.mark.parametrize("column", ["payload", "opened", "sequence", "delete", "root"])
def test_tampered_prepared_blocks_never_become_missing_quote(
    tmp_path: Path, mode: VerificationMode, column: str
) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with prepared(path, ids, request, mode) as history:
        source = ModeledQuotes(history, simulation_policy().pricing, history.evidence.identity)
        runner = runner_for(history, simulation_policy(), request)
        if column == "root":
            history.blocks = (replace(history.blocks[0], content_digest="0" * 64),)
        else:
            assert history._db is not None
            history._db.close()
            with sqlite3.connect(history.path) as writer:
                if column == "delete":
                    writer.execute("DELETE FROM candles WHERE sequence=2")
                elif column == "payload":
                    text = writer.execute(
                        "SELECT payload FROM candles WHERE sequence=2"
                    ).fetchone()[0]
                    writer.execute(
                        "UPDATE candles SET payload=? WHERE sequence=2",
                        (text.replace('"101"', '"102"'),),
                    )
                else:
                    writer.execute(f"UPDATE candles SET {column}=? WHERE sequence=2", (0,))
            history._open_snapshot()
        with pytest.raises(ValueError, match="PREPARED_HISTORY"):
            source.estimate(SYMBOL, LAUNCH + 2 * MINUTE)
        report = runner.run()
        assert report.interruption is not None and "PREPARED_HISTORY" in report.interruption
        assert not any(e.kind == "missing_quote" for e in runner.snapshot.emissions)


def test_partial_cutoff_supports_prefix_and_blocks_unknown_future(tmp_path: Path) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    cutoff = LAUNCH + timedelta(seconds=90)
    with PreparedTradeHistory(path, ids, request, cutoff=cutoff) as history:
        policy = simulation_policy(Scenario.FAVORABLE)
        source = ModeledQuotes(history, policy.pricing, history.evidence.identity)
        assert source.at(SYMBOL, cutoff) is not None
        with pytest.raises(ValueError, match="CUTOFF"):
            source.at(SYMBOL, cutoff + timedelta(microseconds=1))
        runner = runner_for(history, policy, request)
        prefix = runner.run(cutoff)
        assert prefix.interruption is None and prefix.stats.minutes_read == 1
        report = runner.run()
        assert report.interruption is not None and "CUTOFF" in report.interruption
        assert history.evidence.coverage_status == "INCOMPLETE"


def test_warmup_and_allow_entries_remain_explicit(tmp_path: Path) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with PreparedTradeHistory(path, ids, request) as history:
        report = runner_for(
            history, simulation_policy(), replace(request, allow_entries=True)
        ).run()
        assert history.evidence.warmup_status == "INCOMPLETE"
        assert report.signals == report.order_intents == 0
        assert report.financial_results == "FINANCIAL_RESULTS_NOT_AVAILABLE"


def test_policy_and_quote_instance_conflicts_block_runner(tmp_path: Path) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with PreparedTradeHistory(path, ids, request) as history:
        policy = simulation_policy()
        source = ModeledQuotes(history, policy.pricing, history.evidence.identity)
        twin = ModeledQuotes(history, policy.pricing, history.evidence.identity)
        with pytest.raises(ValueError, match="misma fuente QuoteSource"):
            ReplayRunner(
                history, request, quotes=source, simulator=SimulatedExecution(policy, twin)
            )
        changed = replace(policy, pricing=replace(policy.pricing, slippage_bps=Decimal("4")))
        with pytest.raises(ValueError, match="Política de precios"):
            ReplayRunner(
                history, request, quotes=source, simulator=SimulatedExecution(changed, source)
            )


def economic(value: object, report: ReplayReport) -> Any:
    """Comparar todo salvo identidades procedentes de fuente y IDs derivados de ellas."""
    replacements: dict[str, str] = {}
    assert report.simulation is not None
    replacements[report.simulation.identity] = "SOURCE_SIMULATION_IDENTITY"
    assert report.execution_identity is not None
    replacements[report.execution_identity] = "SOURCE_QUOTE_IDENTITY"
    for leg in report.simulation.orders:
        replacements[str(leg.order.order_id)] = f"ORDER_FOR_INTENT_{leg.order.intent_id}"
        for number, fill in enumerate(leg.fills):
            replacements[str(fill.fill_id)] = f"FILL_FOR_INTENT_{leg.order.intent_id}_{number}"
    for quote in report.price_evidence:
        assert isinstance(quote, ModeledQuoteEvidence)
        replacements[quote.dataset_identity] = "SOURCE_DATASET_IDENTITY"

    def normalize(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: normalize(v) for k, v in node.items() if k != "event_digest"}
        if isinstance(node, list):
            return [normalize(v) for v in node]
        if isinstance(node, str):
            return replacements.get(node, node)
        return node

    return normalize(canonical(value))


@dataclass
class RoundCase:
    history: PreparedTradeHistory
    policy: SimulationPolicy
    request: ReplayRequest
    facts: FixtureFacts
    report: ReplayReport
    snapshot: ReplaySnapshot
    sqlite_report: ReplayReport
    sqlite_snapshot: ReplaySnapshot
    candles: tuple[Candle, ...]
    sqlite_path: Path


@pytest.fixture(scope="module", params=[STRICT, SEGMENTED])
def round_case(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    candles: tuple[Candle, ...],
) -> Iterator[RoundCase]:
    rows = round_candles(candles)
    folder = tmp_path_factory.mktemp("historical-modeled-round")
    path, ids = registered(folder, trade_registration(folder, rows))
    sqlite = write_db(folder / "legacy.sqlite3", rows)
    policy = simulation_policy()
    # Exactamente el fixture aprobado de test_simulated_replay y la congelación pre-edición.
    policy = replace(policy, pricing=replace(policy.pricing, quote_latency_ms=0))
    replay = ReplayRequest(START, rows[-1].close_time, True)
    facts = FixtureFacts((ReplayFact(DUE + timedelta(seconds=1), EntryControl(False), 0),))
    with SQLiteHistory(sqlite) as legacy:
        with prepared(path, ids, replay, request.param) as history:
            source = ModeledQuotes(history, policy.pricing, history.evidence.identity)
            # SQLite representa exactamente el dataset seleccionado y verificado del fixture.
            old = ModeledQuotes(legacy, policy.pricing, source._identity_dataset)
            reference = ReplayRunner(
                legacy, replay, quotes=old, simulator=SimulatedExecution(policy, old), facts=facts
            )
            sqlite_report = reference.run()
            sqlite_snapshot = reference.snapshot
            runner = runner_for(history, policy, replay, facts)
            report = runner.run()
            yield RoundCase(
                history,
                policy,
                replay,
                facts,
                report,
                runner.snapshot,
                sqlite_report,
                sqlite_snapshot,
                rows,
                sqlite,
            )


def test_complete_round_equivalence_and_financial_optins(round_case: RoundCase) -> None:
    case = round_case
    report = case.report
    assert report.interruption is None and report.simulation is not None
    assert economic(report, report) == economic(case.sqlite_report, case.sqlite_report)
    assert economic(case.snapshot.emissions, report) == economic(
        case.sqlite_snapshot.emissions, case.sqlite_report
    )
    assert (
        tuple(case.history.read(SYMBOL, case.candles[0].open_time, case.request.end))
        == case.candles
    )
    assert report.execution_identity == case.sqlite_report.execution_identity
    assert report.price_evidence != case.sqlite_report.price_evidence
    strategy = report.final_state.strategy_state
    assert isinstance(strategy, AltBStrategyState)
    assert strategy.base is None and strategy.round_state is None
    assert len(strategy.round_history) == 1
    record = strategy.round_history[0]
    assert record.round_state.status(record.base) == "CLOSED"
    assert record.round_state.quantity_open(record.base) == 0
    assert len(record.base.fills) == 1
    assert all(
        leg.order is not None and leg.order.status is OrderStatus.FILLED
        for leg in record.round_state.dcas
    )
    assert len(record.round_state.dcas[0].fills) > 1
    assert len(record.round_state.take_profits) > 1
    assert any(len(leg.fills) > 1 for leg in record.round_state.take_profits)
    base = report.simulation.orders[0]
    assert base.emitted_at == DUE
    assert base.order.intent.order_type is OrderType.MARKET
    assert all(
        o.order.intent.reduce_only
        for o in report.simulation.orders
        if o.order.intent.side is Side.SELL
    )
    assert all(f.provenance == "MODELED_EXECUTION" for f in report.simulation.facts)
    assert all(v.used <= v.cap for v in report.simulation.liquidity)
    assert report.execution_metrics is not None
    assert report.execution_metrics.cancellations_confirmed >= 1
    assert report.execution_metrics.reconciliation_conflicts == 0
    assert report.financial_results == "FINANCIAL_RESULTS_NOT_AVAILABLE"
    assert all(
        component is None
        for component in (
            case.snapshot.ledger,
            case.snapshot.performance,
            case.snapshot.risk,
            case.snapshot.reserve,
        )
    )
    fills = [f for o in report.simulation.orders for f in o.fills]
    assert len({f.fill_id for f in fills}) == len(fills)
    assert len({o.order.order_id for o in report.simulation.orders}) == len(
        report.simulation.orders
    )


@pytest.mark.parametrize(
    "cut",
    [
        START - timedelta(minutes=800),
        START,
        T,
        DUE - timedelta(microseconds=1),
        DUE,
        DUE + timedelta(milliseconds=50),
        DUE + timedelta(milliseconds=100),
        DUE + timedelta(minutes=4),
        DUE + timedelta(minutes=5, seconds=30),
        DUE + timedelta(minutes=6),
        DUE + timedelta(minutes=9),
        DUE + timedelta(minutes=16),
        DUE + timedelta(minutes=21),
    ],
)
def test_continuous_fragmented_exact_same_prepared_session(
    round_case: RoundCase, cut: datetime
) -> None:
    case = round_case
    part = runner_for(case.history, case.policy, case.request, case.facts)
    part.run(cut)
    if cut == DUE + timedelta(minutes=5, seconds=30):
        assert part.snapshot.simulation is not None
        assert any(
            o.cancellation is not None and o.cancel_confirmed_at is None
            for o in part.snapshot.simulation.orders
        )
    resumed = runner_for(case.history, case.policy, case.request, case.facts, part.snapshot)
    assert resumed.run() == case.report
    assert resumed.snapshot == case.snapshot
    assert resumed.snapshot.price_evidence == case.snapshot.price_evidence
    assert resumed.snapshot.integrity_digest == case.snapshot.integrity_digest
    assert resumed.run() == case.report


@pytest.mark.parametrize("scenario", list(Scenario))
def test_profiles_quotes_and_simulator_match_sqlite(
    round_case: RoundCase, scenario: Scenario
) -> None:
    case = round_case
    policy = simulation_policy(scenario)
    with SQLiteHistory(case.sqlite_path) as legacy:
        new = ModeledQuotes(case.history, policy.pricing, case.history.evidence.identity)
        old = ModeledQuotes(legacy, policy.pricing, new._identity_dataset)
        for candle in case.candles:
            if candle.close_time <= START:
                continue
            for offset in (0, policy.pricing.quote_latency_ms):
                at = candle.close_time + timedelta(milliseconds=offset)
                if at > case.request.end:
                    continue
                expected = old.estimate(SYMBOL, at)
                actual = new.estimate(SYMBOL, at)
                assert actual.reason == expected.reason
                if expected.evidence is None:
                    assert actual.evidence is None
                else:
                    assert actual.evidence == replace(
                        expected.evidence, dataset_identity=case.history.evidence.identity
                    )
                    assert actual.evidence.reference_candle.close_time <= at
        reference = ReplayRunner(
            legacy,
            case.request,
            quotes=old,
            simulator=SimulatedExecution(policy, old),
            facts=case.facts,
        )
        approved = reference.run()
        report = runner_for(case.history, policy, case.request, case.facts).run()
        assert report.interruption is None
        assert economic(report, report) == economic(approved, approved)
        assert report.execution_metrics is not None
        assert report.execution_metrics.scenario == scenario.value


def test_hostile_decimal_and_network_forbidden(
    round_case: RoundCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = round_case

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("5D2 OFFLINE: network forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    source = ModeledQuotes(case.history, case.policy.pricing, case.history.evidence.identity)
    expected = source.estimate(SYMBOL, DUE)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_UP
        ctx.traps[Inexact] = True
        assert source.estimate(SYMBOL, DUE) == expected
        runner = runner_for(case.history, case.policy, case.request, case.facts)
        assert runner.run() == case.report
        assert runner.snapshot == case.snapshot
        assert ctx.prec == 2 and ctx.rounding == ROUND_UP and ctx.traps[Inexact]


def test_entries_off_and_noquotes_default_on_prepared(round_case: RoundCase) -> None:
    case = round_case
    runner = ReplayRunner(case.history, case.request)
    report = runner.run()
    assert isinstance(runner.quotes, NoQuotes)
    assert report.order_intents == 0 and report.simulation is None
    disabled = runner_for(case.history, case.policy, replace(case.request, allow_entries=False))
    assert disabled.run().order_intents == 0


@pytest.mark.parametrize("available", [False, True])
def test_market_requires_valid_prepared_quote_and_slippage_once(
    round_case: RoundCase, available: bool
) -> None:
    case = round_case
    policy = case.policy if available else simulation_policy()
    if not available:
        policy = replace(policy, pricing=replace(policy.pricing, freshness_limit_ms=500))
    source = ModeledQuotes(case.history, policy.pricing, case.history.evidence.identity)
    sim = SimulatedExecution(policy, source)
    state = opened(sim, intent(kind=OrderType.MARKET, qty="1"))
    candle = next(c for c in case.candles if c.close_time == DUE + 2 * MINUTE)
    state = sim.candle(state, candle, candle.close_time)
    fills = state.orders[0].fills
    if available:
        quote = source.at(SYMBOL, candle.close_time)
        assert quote is not None
        assert quote.quote.ask.value == Decimal("100.05")
        assert fills[0].price.value == Decimal("100.080015")
        assert fills[0].quantity.value == Decimal("1")
        assert fills[0].timestamp == candle.close_time
        assert fills[0].timestamp >= state.orders[0].eligible_from > DUE
    else:
        assert not fills and state.audit[-1].reason == "missing_valid_quote"
    terminal = drain(sim, state, candle.close_time + timedelta(seconds=1))
    assert terminal.orders[0].order.status is (
        OrderStatus.FILLED if available else OrderStatus.CANCELED
    )


@pytest.mark.parametrize("eligible", [False, True])
def test_limit_eligibility_and_no_gap_fill_on_prepared(
    round_case: RoundCase, eligible: bool
) -> None:
    case = round_case
    source = ModeledQuotes(case.history, case.policy.pricing, case.history.evidence.identity)
    sim = SimulatedExecution(case.policy, source)
    state = opened(sim, intent(limit="100" if eligible else "98", qty="1"))
    candle = next(c for c in case.candles if c.close_time == DUE + 2 * MINUTE)
    state = sim.candle(state, candle, candle.close_time)
    assert bool(state.orders[0].fills) is eligible
    if eligible:
        assert state.orders[0].fills[0].price == state.orders[0].order.intent.limit_price
    gap = sim.candle(opened(sim, intent(kind=OrderType.MARKET)), None, candle.close_time)
    assert not gap.orders[0].fills
    assert gap.audit[-1].reason == "gap"
    assert not gap.liquidity


def test_strict_and_segmented_economic_identity_with_distinct_scope(round_case: RoundCase) -> None:
    case = round_case
    other_mode = SEGMENTED if case.history.evidence.mode is STRICT else STRICT
    path = case.sqlite_path.parent / "registry.sqlite3"
    ids = tuple(b.dataset_id for b in case.history.evidence.bindings)
    with prepared(path, ids, case.request, other_mode) as other:
        assert other.evidence.identity != case.history.evidence.identity
        assert other.evidence.mode != case.history.evidence.mode
        assert other.evidence.source_state != case.history.evidence.source_state
        assert other.evidence.original_state != case.history.evidence.original_state
        assert other.session is not case.history.session
        first_quotes = ModeledQuotes(
            case.history, case.policy.pricing, case.history.evidence.identity
        )
        second_quotes = ModeledQuotes(other, case.policy.pricing, other.evidence.identity)
        assert first_quotes.identity == second_quotes.identity
        report = runner_for(other, case.policy, case.request, case.facts).run()
        assert economic(report, report) == economic(case.report, case.report)
        assert report.final_state == case.report.final_state
        assert report.simulation is not None and case.report.simulation is not None
        assert report.simulation.orders == case.report.simulation.orders
        assert report.simulation.facts == case.report.simulation.facts
        assert report.price_evidence != case.report.price_evidence
        assert report.event_digest != case.report.event_digest
        with pytest.raises(ValueError, match="Reanudación"):
            runner_for(other, case.policy, case.request, case.facts, case.snapshot)


@pytest.mark.parametrize("reason", ["SOURCE_CONFLICT", "SELECTION_INCOMPATIBLE", "unexpected"])
def test_prepared_read_valueerror_is_never_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    path, ids, request, _ = small_registry(tmp_path)
    with PreparedTradeHistory(path, ids, request) as history:
        source = ModeledQuotes(history, simulation_policy().pricing, history.evidence.identity)
        runner = runner_for(history, simulation_policy(), request)

        def broken(symbol: Symbol, start: datetime, end: datetime) -> Generator[Candle, None, None]:
            raise ValueError(reason)
            yield  # pragma: no cover

        monkeypatch.setattr(history, "read", broken)
        with pytest.raises(ValueError, match=reason):
            source.estimate(SYMBOL, LAUNCH + 2 * MINUTE)
        report = runner.run()
        assert report.interruption == "ValueError:" + reason
        assert not any(e.kind == "missing_quote" for e in runner.snapshot.emissions)


def test_preparation_quotes_and_replay_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, ids, request, _ = small_registry(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("OFFLINE: network forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with PreparedTradeHistory(path, ids, request) as history:
        source = ModeledQuotes(history, simulation_policy().pricing, history.evidence.identity)
        assert source.at(SYMBOL, LAUNCH + 2 * MINUTE) is not None
        assert runner_for(history, simulation_policy(), request).run().interruption is None
