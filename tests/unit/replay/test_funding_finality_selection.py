"""AUD-5D4B-001: available compatible proofs through the public APIs."""

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from time import monotonic, sleep

import pytest

from trading_engine.binance.klines import to_ms
from trading_engine.models.enums import OrderStatus, Side
from trading_engine.models.ids import FillId
from trading_engine.models.market import Candle
from trading_engine.models.orders import OrderIntent
from trading_engine.replay.dynamic_funding import (
    FixtureCalendarSource,
    FixtureExposureFinality,
    FixtureFinalitySource,
    FixtureFundingCalendar,
)
from trading_engine.replay.dynamic_funding.core import DynamicFunding, FundingContext
from trading_engine.replay.dynamic_funding.models import FundingState
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.models import (
    FinancialObservation,
    PerformanceState,
)
from trading_engine.replay.historical_validation.funding_history import PreparedFundingHistory
from trading_engine.replay.historical_validation.funding_source import HistoricalFundingSource
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.verification import SessionPolicy, VerificationMode
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import FillEvidence, FixtureFacts, FixtureQuotes, ReplayFact
from trading_engine.replay.runner import ReplayRunner

from .conftest import DUE, END, START, fill_for, order_for, write_db
from .test_dynamic_funding import (
    FUNDING_AT,
    RECEIVED,
    advance,
    buy,
    component,
    dynamic_policy,
    ledger_for,
)
from .test_financial_performance import policy
from .test_historical_funding import inventory, publication, registered, registration, row

FIXTURE = "SYNTHETIC_FIXTURE_ONLY"


def ids(*facts: ReplayFact) -> tuple[FillId, ...]:
    result = []
    for fact in facts:
        assert isinstance(fact.payload, FillEvidence)
        result.append(fact.payload.fill.fill_id)
    return tuple(result)


def declaration(
    facts: FixtureFacts,
    through: datetime,
    received: datetime,
    fills: tuple[FillId, ...],
    assumptions: str = "Independent complete synthetic execution manifest",
) -> FixtureExposureFinality:
    return FixtureExposureFinality(through, received, facts.identity, fills, assumptions, FIXTURE)


def bind(
    original: DynamicFunding, facts: FixtureFacts, *manifests: FixtureExposureFinality
) -> tuple[DynamicFunding, FundingState]:
    dynamic = replace(original, finality=FixtureFinalitySource(manifests))
    return dynamic, dynamic.initial(START, END, facts, None, FinancialLedger().identity)


def funding_value(state: FundingState, facts: FixtureFacts) -> Decimal | None:
    performance = FinancialPerformance(policy(capital=None))
    result = performance.initial(START, END)
    for event in state.events:
        result = performance.observe(
            result, FinancialObservation(event.received_at, ledger_for(facts, event.received_at))
        )
        result = performance.observe(result, FinancialObservation(event.received_at, event))
    ledger = ledger_for(facts, END)
    result = performance.observe(result, FinancialObservation(END, ledger))
    assert not result.conflicts
    performance.validate(result, END, ledger)
    return performance.value(result, END).funding.value


def snapshot_bytes(snapshot: ReplaySnapshot) -> bytes:
    # Opaque tokens are compared by whole-snapshot equality, never serialized as authority.
    return encoded(
        tuple(
            (field.name, getattr(snapshot, field.name))
            for field in fields(snapshot)
            if field.name not in {"history_session", "mark_history_session", "funding_session"}
        )
    )


def test_a_public_component_uses_complete_proof_behind_incomplete_manifest(tmp_path: Path) -> None:
    first, later = buy(), buy(2, second=240, received=END + timedelta(minutes=1))
    facts = FixtureFacts((first, later))
    wide = declaration(
        facts, START + timedelta(minutes=4), START + timedelta(minutes=5), ids(first, later)
    )
    narrow = declaration(facts, FUNDING_AT, START + timedelta(minutes=6), ids(first))
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(original, facts, wide, narrow)
        prefix = advance(dynamic, state, facts, wide.received_at)
        assert prefix.latest[0].status == "PENDING_EXPOSURE"
        result = advance(dynamic, prefix, facts, END)
        print(
            json.dumps(
                {
                    "api": "DynamicFunding.propose",
                    "status": result.latest[0].status,
                    "funding": str(funding_value(result, facts)),
                    "coverage": bool(result.coverage),
                }
            )
        )
        assert result.latest[0].status == "ASSIGNED"
        proof = result.latest[0].finalization
        assert proof is not None and proof.identity == narrow.identity
        assert funding_value(result, facts) == Decimal("-0.1000")
        assert result.cursor == 1 and result.finality_cursor == 2
        assert result.journal[: len(prefix.journal)] == prefix.journal
        assert advance(dynamic, result, facts, END) == result


def test_b_both_manifests_incomplete_remain_pending(tmp_path: Path) -> None:
    first = buy(received=END + timedelta(minutes=1))
    later = buy(2, second=240, received=END + timedelta(minutes=2))
    facts = FixtureFacts((first, later))
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(
            original,
            facts,
            declaration(
                facts, START + timedelta(minutes=4), START + timedelta(minutes=5), ids(first, later)
            ),
            declaration(facts, FUNDING_AT, START + timedelta(minutes=6), ids(first)),
        )
        result = advance(dynamic, state, facts, END)
        assert result.latest[0].status == "PENDING_EXPOSURE"
        assert result.latest[0].finalization is None and not result.events and not result.coverage


def test_c_first_complete_manifest_keeps_its_original_proof(tmp_path: Path) -> None:
    first, later = buy(), buy(2, second=240, received=END + timedelta(minutes=1))
    facts = FixtureFacts((first, later))
    narrow = declaration(facts, FUNDING_AT, RECEIVED, ids(first))
    wide = declaration(
        facts, START + timedelta(minutes=4), START + timedelta(minutes=5), ids(first, later)
    )
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(original, facts, narrow, wide)
        prefix = advance(dynamic, state, facts, RECEIVED)
        proof = prefix.latest[0].finalization
        assert proof is not None and proof.identity == narrow.identity
        result = advance(dynamic, prefix, facts, END)
        assert result.latest == prefix.latest and result.journal == prefix.journal
        assert result.latest[0].payment == prefix.latest[0].payment
        assert funding_value(result, facts) == Decimal("-0.1000")


@pytest.mark.parametrize("tie", ["receipt", "frontier", "identity"])
@pytest.mark.parametrize("reverse", [False, True])
def test_d_complete_candidates_have_explicit_canonical_order(
    tmp_path: Path, tie: str, reverse: bool
) -> None:
    first = buy()
    facts = FixtureFacts((first,))
    a = declaration(facts, FUNDING_AT, RECEIVED, ids(first), "Manifest A")
    b = replace(a, assumptions="Manifest B")
    if tie == "receipt":
        b = replace(b, received_at=RECEIVED + timedelta(seconds=1))
    elif tie == "frontier":
        b = replace(b, through=FUNDING_AT + timedelta(seconds=1))
    expected = min((a, b), key=lambda e: (e.received_at, e.through, e.identity))
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(original, facts, *((b, a) if reverse else (a, b)))
        result = advance(dynamic, state, facts, END)
        proof = result.latest[0].finalization
        assert proof is not None and proof.identity == expected.identity
        assert len([d for d in result.journal if d.payment is not None]) == 1
        assert advance(dynamic, result, facts, END) == result


@pytest.mark.parametrize("reverse", [False, True])
def test_e_contradictory_manifest_is_not_hidden_by_complete_candidate(
    tmp_path: Path, reverse: bool
) -> None:
    first = buy()
    facts = FixtureFacts((first,))
    good = declaration(facts, FUNDING_AT, RECEIVED, ids(first))
    bad = replace(good, through=FUNDING_AT + timedelta(seconds=1), fill_ids=())
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(original, facts, *((bad, good) if reverse else (good, bad)))
        frozen = encoded(state)
        with pytest.raises(ValueError, match="UNDECLARED_FILLS|MANIFEST_CONTRADICTION"):
            advance(dynamic, state, facts, END)
        assert encoded(state) == frozen and not state.events


def test_f_future_complete_manifest_cannot_finalize_early(tmp_path: Path) -> None:
    first, later = buy(), buy(2, second=240, received=END + timedelta(minutes=1))
    facts = FixtureFacts((first, later))
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(
            original,
            facts,
            declaration(
                facts, START + timedelta(minutes=4), START + timedelta(minutes=5), ids(first, later)
            ),
            declaration(facts, FUNDING_AT, END + timedelta(seconds=1), ids(first)),
        )
        result = advance(dynamic, state, facts, END)
        assert result.finality_cursor == 1 and result.latest[0].status == "PENDING_EXPOSURE"
        assert not result.events and not result.coverage


def test_e_previously_received_contradiction_is_checked_even_with_a_complete_proof(
    tmp_path: Path,
) -> None:
    first = buy(received=START + timedelta(minutes=8))
    facts = FixtureFacts((first,))
    wide = declaration(facts, FUNDING_AT + timedelta(seconds=1), RECEIVED, ids(first))
    narrow = declaration(facts, FUNDING_AT, RECEIVED + timedelta(seconds=1), ())
    with component(tmp_path, facts, received=END) as (original, _):
        dynamic, state = bind(original, facts, wide, narrow)
        prefix = advance(dynamic, state, facts, START + timedelta(minutes=4))
        assert prefix.finality_cursor == 2 and prefix.cursor == 0 and not prefix.journal
        frozen = encoded(prefix)
        with pytest.raises(ValueError, match="UNDECLARED_FILLS"):
            advance(dynamic, prefix, facts, END)
        assert encoded(prefix) == frozen


def test_foreign_manifest_is_rejected_before_state_publication(tmp_path: Path) -> None:
    first = buy()
    facts = FixtureFacts((first,))
    foreign = replace(declaration(facts, FUNDING_AT, RECEIVED, ids(first)), fact_identity="foreign")
    with component(tmp_path, facts) as (original, _):
        with pytest.raises(ValueError, match="SOURCE_BINDING_INVALID"):
            bind(original, facts, foreign)


def test_e_late_fill_cannot_contradict_an_unselected_received_manifest(tmp_path: Path) -> None:
    first = buy()
    promised = buy(2, second=240, received=END + timedelta(minutes=1))
    omitted = buy(3, second=180, received=START + timedelta(minutes=7))
    facts = FixtureFacts((first, promised, omitted))
    wide = declaration(
        facts, START + timedelta(minutes=4), START + timedelta(minutes=5), ids(first, promised)
    )
    narrow = declaration(facts, FUNDING_AT, START + timedelta(minutes=6), ids(first))
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(original, facts, wide, narrow)
        prefix = advance(dynamic, state, facts, narrow.received_at)
        assert prefix.latest[0].status == "ASSIGNED"
        frozen = encoded(prefix)
        with pytest.raises(ValueError, match="LATE_FILL_CONTRADICTS_FINALITY"):
            dynamic.validate_fact(prefix, omitted, ledger_for(facts, narrow.received_at))
        assert encoded(prefix) == frozen


def test_g_second_manifest_can_finalize_zero_without_zero_payment(tmp_path: Path) -> None:
    later = buy(second=240, received=END + timedelta(minutes=1))
    facts = FixtureFacts((later,))
    narrow = declaration(facts, FUNDING_AT, START + timedelta(minutes=6), ())
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(
            original,
            facts,
            declaration(
                facts, START + timedelta(minutes=4), START + timedelta(minutes=5), ids(later)
            ),
            narrow,
        )
        result = advance(dynamic, state, facts, END)
        decision = result.latest[0]
        assert decision.status == "NO_EXPOSURE" and decision.quantity == 0
        assert decision.payment is None and decision.finalization is not None
        assert decision.finalization.identity == narrow.identity
        assert not any(d.payment is not None for d in result.journal)
        assert funding_value(result, facts) == 0


def test_i_late_funding_uses_closed_historical_round_after_reopening(tmp_path: Path) -> None:
    first = buy()
    close = buy(2, second=140, side=Side.SELL)
    reopened = buy(3, second=160, round_number=200)
    late = buy(4, second=240, round_number=200, received=END + timedelta(minutes=1))
    facts = FixtureFacts((first, close, reopened, late))
    with component(tmp_path, facts) as (original, _):
        dynamic, state = bind(
            original,
            facts,
            declaration(
                facts,
                START + timedelta(minutes=4),
                START + timedelta(minutes=5),
                ids(first, close, reopened, late),
            ),
            declaration(facts, FUNDING_AT, START + timedelta(minutes=6), ids(first)),
        )
        first_payload = first.payload
        assert isinstance(first_payload, FillEvidence)
        historical_round = first_payload.order.intent.round_id
        ledger = FinancialLedger().reconcile_closed(
            ledger_for(facts, END), (historical_round,), at=END
        )
        assert ledger.round_ledger[0].closed_at == END
        assert ledger.round_ledger[1].open_quantity == 10
        result, _ = dynamic.propose(
            state,
            FundingContext(END, START, END, ledger, facts.identity, END + timedelta(minutes=1)),
        )
        assert result.latest[0].status == "ASSIGNED"
        assert result.latest[0].round_id == historical_round and result.latest[0].quantity == 10
        assert funding_value(result, facts) == Decimal("-0.1000")


@dataclass(frozen=True)
class PublicCase:
    market: SQLiteHistory
    request: ReplayRequest
    facts: FixtureFacts
    quotes: FixtureQuotes
    dynamic: DynamicFunding
    wide: FixtureExposureFinality
    narrow: FixtureExposureFinality
    prepared: PreparedFundingHistory
    session: SealedSegmentSession | None
    source_file: Path

    def runner(
        self, snapshot: ReplaySnapshot | None = None, *, enabled: bool = True
    ) -> ReplayRunner:
        return ReplayRunner(
            self.market,
            self.request,
            quotes=self.quotes,
            facts=self.facts,
            ledger=FinancialLedger(),
            performance=FinancialPerformance(policy(capital=None)),
            dynamic_funding=self.dynamic if enabled else None,
            snapshot=snapshot,
        )


@contextmanager
def public_case(
    folder: Path,
    candles: tuple[Candle, ...],
    quotes: FixtureQuotes,
    base_fact: ReplayFact,
    *,
    sealed: bool = False,
    contradict_late: bool = False,
    contradict_unselected: bool = False,
) -> Iterator[PublicCase]:
    db = write_db(folder / "market.sqlite3", candles)
    request = ReplayRequest(START, END, True)
    with ExitStack() as stack:
        market = stack.enter_context(SQLiteHistory(db))
        bootstrap = ReplayRunner(
            market,
            request,
            quotes=quotes,
            facts=FixtureFacts((base_fact,)),
            ledger=FinancialLedger(),
        )
        bootstrap.run(base_fact.received_at)
        assert bootstrap.snapshot.interruption is None
        base_payload = base_fact.payload
        assert isinstance(base_payload, FillEvidence)
        children = tuple(
            e.payload
            for e in bootstrap.snapshot.emissions
            if e.kind == "order_intent"
            and isinstance(e.payload, OrderIntent)
            and e.payload.side is Side.BUY
            and e.payload.intent_id != base_payload.order.intent_id
        )
        child = children[0]
        executed = DUE + (timedelta(seconds=15) if contradict_late else timedelta(minutes=2))
        assert child.limit_price is not None
        order = order_for(child, 9000, executed, OrderStatus.FILLED, child.quantity.value)
        delivered = (
            DUE + timedelta(minutes=2, seconds=40)
            if contradict_late
            else END + timedelta(minutes=1)
        )
        later = ReplayFact(
            delivered, FillEvidence(fill_for(order, 9001, str(child.limit_price.value)), order), 1
        )
        facts = FixtureFacts((base_fact, later))
        if contradict_unselected:
            another = children[1]
            assert another.limit_price is not None
            extra_order = order_for(
                another,
                9002,
                DUE + timedelta(minutes=1),
                OrderStatus.FILLED,
                another.quantity.value,
            )
            extra = ReplayFact(
                DUE + timedelta(minutes=2, seconds=40),
                FillEvidence(
                    fill_for(extra_order, 9003, str(another.limit_price.value)), extra_order
                ),
                2,
            )
            facts = FixtureFacts((base_fact, later, extra))
        funding_t = DUE + timedelta(seconds=30)
        inv = replace(inventory(start=START, end=END), expected_timestamps=(funding_t,))
        rate = row(17, start=START, available=DUE + timedelta(minutes=1))
        rate["fundingTime"] = to_ms(funding_t)
        reg = registration(folder, [rate], inv, start=START, end=END)
        path, dataset_ids = registered(folder, reg)
        mode = VerificationMode.SEALED_SEGMENT_SESSION if sealed else VerificationMode.STRICT_FULL
        session = (
            stack.enter_context(
                SealedSegmentSession(
                    path,
                    dataset_ids,
                    session_identity="aud-5d4b-001",
                    opened_at=END,
                    policy=SessionPolicy(10, 1000, "Independent local synthetic immutable fixture"),
                )
            )
            if sealed
            else None
        )
        prepared = stack.enter_context(
            PreparedFundingHistory(
                path,
                dataset_ids,
                START,
                END,
                cutoff=END,
                inventory=inv,
                mode=mode,
                session=session,
            )
        )
        calendars = FixtureCalendarSource(
            (
                FixtureFundingCalendar(
                    START,
                    END,
                    (funding_t,),
                    END,
                    prepared.evidence.economic_identity,
                    "Independent closed synthetic funding calendar",
                    FIXTURE,
                ),
            )
        )
        wide = declaration(
            facts, DUE + timedelta(minutes=2), DUE + timedelta(minutes=2), ids(base_fact, later)
        )
        narrow = declaration(
            facts, funding_t, DUE + timedelta(minutes=2, seconds=30), ids(base_fact)
        )
        dynamic = DynamicFunding(
            HistoricalFundingSource(prepared, publication()),
            dynamic_policy(),
            FixtureFinalitySource((wide, narrow)),
            calendars,
        )
        yield PublicCase(
            market,
            request,
            facts,
            quotes,
            dynamic,
            wide,
            narrow,
            prepared,
            session,
            Path(reg.original_file),
        )


@pytest.fixture
def finality_case(
    tmp_path: Path, candles: tuple[Candle, ...], quotes: FixtureQuotes, base_fact: ReplayFact
) -> Iterator[PublicCase]:
    with public_case(tmp_path, candles, quotes, base_fact) as case:
        yield case


def test_a_public_runner_assigns_funding_with_the_second_complete_proof(
    finality_case: PublicCase,
) -> None:
    runner = finality_case.runner()
    report = runner.run()
    assert report.interruption is None and report.dynamic_funding is not None
    assert report.financial_performance is not None
    value = report.financial_performance.current.funding.value
    decision = report.dynamic_funding.decisions[0]
    print(
        json.dumps(
            {
                "api": "ReplayRunner.run",
                "status": decision.status,
                "funding": str(value),
                "coverage": bool(report.dynamic_funding.coverage),
            }
        )
    )
    assert decision.status == "ASSIGNED" and decision.finalization is not None
    assert decision.finalization.identity == finality_case.narrow.identity
    payload = finality_case.facts.facts[0].payload
    assert isinstance(payload, FillEvidence)
    with localcontext(ledger_context()):
        assert value == -(payload.fill.quantity.value * Decimal(".01"))
    assert decision.round_id == payload.order.intent.round_id
    assert report.dynamic_funding.coverage
    assert runner.snapshot.funding is not None and runner.snapshot.funding.cursor == 1
    assert runner.snapshot.ledger is not None and len(runner.snapshot.ledger.entries) == 1


def test_h_late_contradictory_fill_rolls_back_without_rewriting_chosen_proof(
    tmp_path: Path, candles: tuple[Candle, ...], quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    with public_case(tmp_path, candles, quotes, base_fact, contradict_late=True) as case:
        runner = case.runner()
        runner.run(case.narrow.received_at)
        prefix = runner.snapshot
        assert prefix.funding is not None and prefix.funding.latest[0].status == "ASSIGNED"
        frozen = snapshot_bytes(prefix)
        with pytest.raises(ValueError, match="LATE_FILL_CONTRADICTS_FINALITY"):
            runner.run()
        assert runner.snapshot == prefix and snapshot_bytes(runner.snapshot) == frozen
        assert runner.snapshot.ledger is not None and not runner.snapshot.ledger.conflicts


def test_e_public_runner_rejects_late_contradiction_of_unselected_manifest(
    tmp_path: Path, candles: tuple[Candle, ...], quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    with public_case(tmp_path, candles, quotes, base_fact, contradict_unselected=True) as case:
        runner = case.runner()
        runner.run(case.narrow.received_at)
        prefix = runner.snapshot
        assert prefix.funding is not None and prefix.funding.latest[0].status == "ASSIGNED"
        frozen = snapshot_bytes(prefix)
        with pytest.raises(ValueError, match="LATE_FILL_CONTRADICTS_FINALITY"):
            runner.run()
        assert runner.snapshot == prefix and snapshot_bytes(runner.snapshot) == frozen
        assert runner.snapshot.ledger is not None and not runner.snapshot.ledger.conflicts


def test_j_continuous_fragmented_and_restored_are_exactly_identical(
    finality_case: PublicCase,
) -> None:
    case = finality_case
    continuous = case.runner()
    expected = continuous.run()
    assert expected.dynamic_funding is not None
    assert expected.dynamic_funding.decisions[0].status == "ASSIGNED"
    fragmented, restored = case.runner(), case.runner()
    for cut in (
        DUE,
        DUE + timedelta(minutes=1),
        case.wide.received_at,
        case.narrow.received_at - timedelta(milliseconds=1),
        case.narrow.received_at,
        END,
    ):
        fragmented.run(cut)
        restored.run(cut)
        restored = case.runner(restored.snapshot)
    assert fragmented.report() == restored.report() == expected
    assert fragmented.snapshot == restored.snapshot == continuous.snapshot
    assert (
        snapshot_bytes(fragmented.snapshot)
        == snapshot_bytes(restored.snapshot)
        == snapshot_bytes(continuous.snapshot)
    )
    assert fragmented.snapshot.integrity_digest == continuous.snapshot.integrity_digest


@pytest.mark.parametrize("stage", ["selection", "before_commit"])
@pytest.mark.parametrize(
    "fault,error",
    [
        ("prepared_close", "PREPARED_FUNDING_NOT_AVAILABLE"),
        ("session_close", "VERIFICATION_NOT_AVAILABLE"),
        ("source_changed", "SOURCE_CHANGED"),
        ("real_ttl", "SESSION_EXPIRED"),
    ],
)
def test_k_trust_loss_preserves_the_complete_prior_tick(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    quotes: FixtureQuotes,
    base_fact: ReplayFact,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    fault: str,
    error: str,
) -> None:
    with public_case(tmp_path, candles, quotes, base_fact, sealed=True) as case:
        runner = case.runner()
        runner.run(case.narrow.received_at - timedelta(milliseconds=1))
        prefix = runner.snapshot
        frozen = snapshot_bytes(prefix)
        original_property = vars(FixtureExposureFinality)["identity"]
        assert isinstance(original_property, property)
        original_identity = original_property.fget
        original_observe = FinancialPerformance.observe
        triggered = False

        def invalidate() -> None:
            nonlocal triggered
            if triggered:
                return
            triggered = True
            case.dynamic.check()
            if fault == "prepared_close":
                case.prepared.close()
            elif fault == "session_close":
                assert case.session is not None
                case.session.close()
            elif fault == "source_changed":
                with case.source_file.open("a") as stream:
                    stream.write("\n")
            else:
                assert case.session is not None
                remaining = case.session._started + case.session.policy.max_seconds - monotonic()
                assert remaining > 0
                sleep(remaining + 0.02)

        def identity(self: FixtureExposureFinality) -> str:
            assert original_identity is not None
            result: str = original_identity(self)
            if self == case.narrow:
                invalidate()
            return result

        def observe(
            self: FinancialPerformance, state: PerformanceState, observation: FinancialObservation
        ) -> PerformanceState:
            result = original_observe(self, state, observation)
            if observation.at == case.narrow.received_at and observation.payload == "VALUE":
                invalidate()
            return result

        if stage == "selection":
            monkeypatch.setattr(FixtureExposureFinality, "identity", property(identity))
        else:
            monkeypatch.setattr(FinancialPerformance, "observe", observe)
        with pytest.raises(ValueError, match=error):
            runner.run(case.narrow.received_at)
        assert triggered and runner.snapshot == prefix and snapshot_bytes(runner.snapshot) == frozen
        assert runner.snapshot.ledger is not None and not runner.snapshot.ledger.conflicts
        assert runner.snapshot.performance is not None and not runner.snapshot.performance.conflicts


def test_l_opt_out_keeps_the_entire_legacy_snapshot(finality_case: PublicCase) -> None:
    case = finality_case
    off = case.runner(enabled=False)
    expected = off.run()
    explicit = ReplayRunner(
        case.market,
        case.request,
        quotes=case.quotes,
        facts=case.facts,
        ledger=FinancialLedger(),
        performance=FinancialPerformance(policy(capital=None)),
        dynamic_funding=None,
    )
    assert explicit.run() == expected and explicit.snapshot == off.snapshot
    assert expected.dynamic_funding is None and off.snapshot.funding is None
    assert off.snapshot.funding_identity is None
