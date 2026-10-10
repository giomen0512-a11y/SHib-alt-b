"""Opt-in, ronda completa y snapshots cotejados con las fuentes originales."""

import json
from collections.abc import Generator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from trading_engine.models.market import Candle
from trading_engine.replay.encoding import encoded
from trading_engine.replay.execution_model.quotes import ModeledQuotes
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.isolated_margin_risk import IsolatedMarginRisk
from trading_engine.replay.isolated_margin_risk.evidence import (
    ConfirmedMarginSource,
    FixtureRiskSource,
)
from trading_engine.replay.models import ReplayReport, ReplaySnapshot
from trading_engine.replay.reserve_transfer_simulation import (
    ReservationPolicy,
    ReserveTransferSimulation,
)
from trading_engine.replay.reserve_transfer_simulation.evidence import (
    CapitalPartitionEvidence,
    FixtureReserveSource,
    TransferCommandEvidence,
)
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.simulated_execution import SimulatedExecution

from .conftest import DUE, START
from .test_financial_performance import MODELED
from .test_isolated_margin_risk import stamp
from .test_margin_risk_replay import RiskRound
from .test_margin_risk_replay import risk_round as baseline_fixture
from .test_reserve_transfer_simulation import auto


@pytest.fixture(scope="module")
def risk_round(
    tmp_path_factory: pytest.TempPathFactory, candles: tuple[Candle, ...]
) -> Generator[RiskRound, None, None]:
    yield from baseline_fixture._get_wrapped_function()(tmp_path_factory, candles)


@dataclass(frozen=True)
class ReserveRound:
    previous: RiskRound
    controller: ReserveTransferSimulation
    risk: IsolatedMarginRisk
    expected: ReplayReport
    final: ReplaySnapshot

    def runner(self, snapshot: ReplaySnapshot | None = None) -> ReplayRunner:
        old = self.previous
        quotes = ModeledQuotes(old.history, old.pricing.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
        return ReplayRunner(
            old.history,
            old.request,
            quotes=quotes,
            facts=old.facts,
            simulator=SimulatedExecution(old.pricing, quotes),
            ledger=FinancialLedger(),
            performance=old.performance,
            risk=self.risk,
            reserve_controller=self.controller,
            snapshot=snapshot,
        )


@pytest.fixture(scope="module")
def reserve_round(risk_round: RiskRound) -> ReserveRound:
    old = risk_round
    assert old.expected.financial_ledger is not None
    assert old.performance.policy.capital is not None
    balance = old.performance.policy.capital.balance_usdc
    capital = CapitalPartitionEvidence(stamp("capital", at=START), balance, balance, (), (), (), ())
    rid = old.expected.financial_ledger.entries[0].round_id
    source = FixtureReserveSource(
        (
            TransferCommandEvidence(
                stamp("manual-request", at=DUE + timedelta(minutes=5)),
                "manual-100",
                rid,
                "PROPOSE",
                Decimal(100),
            ),
        )
    )
    controller = ReserveTransferSimulation(
        ReservationPolicy(MODELED, "CAUSAL_FRESH_MARK"),
        auto(latency=60000, enabled=False),
        capital,
        source,
    )
    risk = IsolatedMarginRisk(
        old.risk.policy, FixtureRiskSource(tuple(old.risk.source.read(0, START)))
    )
    seed = ReserveRound(old, controller, risk, old.expected, old.final)
    runner = seed.runner()
    report = runner.run()
    return replace(seed, expected=report, final=runner.snapshot)


def test_frozen_approved_4a4a_fingerprints(risk_round: RiskRound) -> None:
    expected = json.loads(
        Path("tests/unit/replay/fixtures/reserve_disabled_4a4a_baseline.json").read_text()
    )
    actual = {
        "report": sha256(encoded(risk_round.expected)).hexdigest(),
        "snapshot": risk_round.final.calculate_integrity(),
        "event_digest": risk_round.expected.event_digest,
        "financial_ledger": sha256(encoded(risk_round.expected.financial_ledger)).hexdigest(),
        "financial_performance": sha256(
            encoded(risk_round.expected.financial_performance)
        ).hexdigest(),
        "simulation": sha256(encoded(risk_round.expected.simulation)).hexdigest(),
        "strategy": sha256(encoded(risk_round.expected.final_state)).hexdigest(),
    }
    assert actual == expected
    assert "reserve_transfer_simulation" not in risk_round.expected.as_dict()


def test_case_j_complete_round_only_reserves_and_margin_risk_change(
    reserve_round: ReserveRound,
) -> None:
    report, old = reserve_round.expected, reserve_round.previous.baseline
    assert report.interruption is None
    assert report.final_state == old.final_state
    assert report.financial_ledger == old.financial_ledger
    assert report.financial_performance == old.financial_performance
    assert report.simulation == old.simulation
    assert (
        report.order_intents == old.order_intents and report.cancel_requests == old.cancel_requests
    )
    assert report.reserve_transfer_simulation is not None
    state = report.reserve_transfer_simulation.state
    assert not state.conflicts and state.partition.status == "RECONCILED"
    assert state.partition.pending_order_holds.value == 0
    assert state.partition.pending_transfer_holds.value == 0
    assert len(state.adjustments) == 1 and state.adjustments[0].amount_usdc == 100
    assert state.transfers[0].state == "CONFIRMED_SIMULATED"
    assert state.transfers[0].confirmed_at == DUE + timedelta(minutes=6)
    assert report.isolated_margin_risk is not None and report.financial_performance is not None
    final = report.isolated_margin_risk.current.positions[0]
    assert final.open_quantity == 0
    assert final.initial_assigned.value == final.initial_released.value
    assert final.additional_assigned.value == 100
    assert (
        state.partition.account_wallet == report.financial_performance.current.wallet
        or state.partition.account_wallet.value == report.financial_performance.current.wallet.value
    )
    assert state.partition.isolated_wallet_allocated.value == final.isolated_wallet_balance.value
    assert all(r.hold.value == 0 for r in state.reservations)
    assert any(r.cancel_requested for r in state.reservations)
    assert any(len(r.fill_ids) > 1 for r in state.reservations)


@pytest.mark.parametrize(
    "cut",
    [
        DUE,
        DUE + timedelta(minutes=5, seconds=30),
        DUE + timedelta(minutes=6),
        DUE + timedelta(minutes=6, milliseconds=1),
        DUE + timedelta(minutes=12, milliseconds=333),
    ],
)
def test_segmented_equals_continuous_before_and_after_confirmation(
    reserve_round: ReserveRound, cut: datetime
) -> None:
    first = reserve_round.runner()
    first.run(cut)
    restored = reserve_round.runner(first.snapshot)
    actual = restored.run()
    assert actual == reserve_round.expected
    assert restored.snapshot == reserve_round.final
    assert actual.event_digest == reserve_round.expected.event_digest


@pytest.mark.parametrize(
    "field",
    [
        "partition",
        "transfers",
        "adjustments",
        "queue",
        "processed_fill_ids",
        "journal",
        "source_cursor",
        "risk",
        "settled_partition",
    ],
)
def test_snapshot_forgery_even_with_recomputed_checksum_rejected(
    reserve_round: ReserveRound, field: str
) -> None:
    runner = reserve_round.runner()
    runner.run(DUE + timedelta(minutes=5, seconds=30))
    snapshot = runner.snapshot
    assert snapshot.reserve is not None
    st = snapshot.reserve
    if field == "adjustments":
        snapshot = reserve_round.final
        assert snapshot.reserve is not None
        st = snapshot.reserve
    states = {
        "partition": replace(
            st,
            partition=replace(
                st.partition, free_balance=replace(st.partition.free_balance, value=Decimal(99999))
            ),
        ),
        "settled_partition": replace(
            st,
            settled_partition=replace(
                st.partition, free_balance=replace(st.partition.free_balance, value=Decimal(99999))
            ),
        ),
        "transfers": replace(st, transfers=()),
        "adjustments": replace(st, adjustments=()),
        "queue": replace(st, queue=()),
        "processed_fill_ids": replace(st, processed_fill_ids=()),
        "journal": replace(st, journal=()),
        "source_cursor": replace(st, source_cursor=99),
        "risk": replace(st, risk=None),
    }
    bad = replace(snapshot, reserve=states[field])
    bad = replace(bad, integrity_digest=bad.calculate_integrity())
    with pytest.raises(ValueError, match="adulterado"):
        reserve_round.runner(bad)


def test_generated_adjustments_use_explicit_risk_source_validation(
    reserve_round: ReserveRound,
) -> None:
    snapshot = reserve_round.final
    assert (
        snapshot.reserve is not None
        and snapshot.performance is not None
        and snapshot.risk is not None
    )
    source = ConfirmedMarginSource(reserve_round.controller.identity, snapshot.reserve.adjustments)
    processed = reserve_round.risk.validate(
        snapshot.risk,
        snapshot.clock.now,
        reserve_round.previous.performance,
        snapshot.performance,
        confirmations=source,
    )
    assert processed == snapshot.risk_cursor
    with pytest.raises(ValueError, match="adulterado"):
        reserve_round.risk.validate(
            snapshot.risk,
            snapshot.clock.now,
            reserve_round.previous.performance,
            snapshot.performance,
        )


def test_source_not_anticipated(reserve_round: ReserveRound) -> None:
    runner = reserve_round.runner()
    runner.run(DUE + timedelta(minutes=4, seconds=59))
    st = runner.snapshot.reserve
    assert st is not None
    assert st.source_cursor == 0 and not st.transfers and not st.adjustments
    assert all(e.stamp.received_at <= runner.snapshot.clock.now for e in st.journal)


@pytest.mark.parametrize("volume", ["0", "1"])
def test_shortfill_preserves_execution_and_confirmed_exposure_with_controller(
    tmp_path: Path, candles: tuple[Candle, ...], volume: str
) -> None:
    from trading_engine.replay.financial_performance import FinancialPerformance
    from trading_engine.replay.history import SQLiteHistory
    from trading_engine.replay.models import ReplayRequest
    from trading_engine.replay.ports import FixtureFacts
    from trading_engine.strategy.alt_b.events.state import AltBStrategyState

    from .conftest import END, write_db
    from .test_financial_performance import policy
    from .test_isolated_margin_risk import margin_policy
    from .test_margin_risk_replay import risk_runner
    from .test_simulated_execution import simulation_policy
    from .test_simulated_replay import round_candles

    at = DUE + timedelta(minutes=2)
    rows = tuple(
        replace(c, volume=Decimal(volume), taker_buy_base_volume=Decimal(volume) / Decimal(2))
        if c.close_time == at
        else c
        for c in round_candles(candles)
    )
    database = write_db(tmp_path / "shortfill-reserves.sqlite3", rows)
    pricing = simulation_policy()
    pricing = replace(pricing, pricing=replace(pricing.pricing, quote_latency_ms=0))
    capital = CapitalPartitionEvidence(
        stamp("capital", at=START), Decimal(1000), Decimal(1000), (), (), (), ()
    )
    controller = ReserveTransferSimulation(
        ReservationPolicy(MODELED, "CAUSAL_FRESH_MARK"), auto(enabled=False), capital
    )
    request = ReplayRequest(START, END, True)
    performance = FinancialPerformance(policy())
    risk = IsolatedMarginRisk(margin_policy())
    with SQLiteHistory(database) as history:
        baseline = risk_runner(history, pricing, request, FixtureFacts(), performance, risk).run()
        quotes = ModeledQuotes(history, pricing.pricing, "SYNTHETIC_ROUND_FIXTURE_ONLY")
        runner = ReplayRunner(
            history,
            request,
            quotes=quotes,
            simulator=SimulatedExecution(pricing, quotes),
            ledger=FinancialLedger(),
            performance=performance,
            risk=risk,
            reserve_controller=controller,
        )
        report = runner.run()
    assert report.interruption is None
    assert report.final_state == baseline.final_state and report.simulation == baseline.simulation
    assert report.financial_ledger == baseline.financial_ledger
    assert report.financial_performance == baseline.financial_performance
    assert report.isolated_margin_risk == baseline.isolated_margin_risk
    assert report.reserve_transfer_simulation is not None
    state = report.reserve_transfer_simulation.state
    assert not state.transfers and not state.adjustments
    assert all(r.hold.value == 0 for r in state.reservations)
    if volume == "1":
        strategy = report.final_state.strategy_state
        assert isinstance(strategy, AltBStrategyState) and strategy.base is not None
        assert strategy.base.block_reason == "base_shortfill_reconciliation"
        assert state.risk is not None and state.risk.positions[0].open_quantity == Decimal(".05")
        assert state.partition.free_balance.value is None


def test_opt_in_requires_financial_risk_component(reserve_round: ReserveRound) -> None:
    with pytest.raises(ValueError, match="requieren IsolatedMarginRisk"):
        ReplayRunner(
            reserve_round.previous.history,
            reserve_round.previous.request,
            reserve_controller=reserve_round.controller,
        )


def test_source_identity_and_future_initial_evidence_rejected(reserve_round: ReserveRound) -> None:
    first = reserve_round.runner()
    first.run(DUE)
    command = reserve_round.controller.source.events[0]
    assert isinstance(command, TransferCommandEvidence)
    source = replace(command, request_id="foreign-source")
    other = replace(reserve_round.controller, source=FixtureReserveSource((source,)))
    bad = replace(reserve_round, controller=other)
    with pytest.raises(ValueError, match="misma sesión"):
        bad.runner(first.snapshot)
    assert reserve_round.controller.capital is not None
    capital = replace(
        reserve_round.controller.capital,
        stamp=stamp("future-capital", at=START + timedelta(seconds=1)),
    )
    bad = replace(reserve_round, controller=replace(reserve_round.controller, capital=capital))
    with pytest.raises(ValueError, match="exactamente en start"):
        bad.runner()
