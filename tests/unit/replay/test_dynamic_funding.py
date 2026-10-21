"""Contratos económicos y adversarios 5D4B con fuentes fixture originales."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from trading_engine.models.enums import Side
from trading_engine.models.ids import RoundId
from trading_engine.replay.dynamic_funding import (
    DynamicFundingPolicy,
    FixtureCalendarSource,
    FixtureExposureFinality,
    FixtureFinalitySource,
    FixtureFundingCalendar,
)
from trading_engine.replay.dynamic_funding.core import DynamicFunding, FundingContext
from trading_engine.replay.dynamic_funding.models import FundingState
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import CoverageEvidence, FundingEvidence
from trading_engine.replay.financial_performance.models import FinancialObservation
from trading_engine.replay.historical_validation.funding_source import HistoricalFundingSource
from trading_engine.replay.historical_validation.verification import VerificationMode
from trading_engine.replay.ports import FillEvidence, FixtureFacts, ReplayFact

from .conftest import END, START
from .test_financial_ledger import execution
from .test_financial_performance import policy
from .test_historical_funding import inventory, prepared, publication, registered, registration, row

EMPTY_FACTS = FixtureFacts()
FUNDING_AT = START + timedelta(minutes=2)
RECEIVED = START + timedelta(minutes=3)


def dynamic_policy(**changes: Any) -> DynamicFundingPolicy:
    return replace(
        DynamicFundingPolicy(
            "SYNTHETIC_FIXTURE_ONLY",
            "FIXTURE_MARK_AT_FUNDING_TIME",
            "Declared synthetic reference at exact T",
            512,
            2048,
            4096,
            44640,
        ),
        **changes,
    )


def buy(
    number: int = 1,
    *,
    second: int = 30,
    quantity: str = "10",
    round_number: int = 100,
    side: Side = Side.BUY,
    received: datetime | None = None,
) -> ReplayFact:
    return execution(
        number,
        side,
        quantity,
        "100",
        timestamp=START + timedelta(seconds=second),
        received_at=received,
        round_number=round_number,
    )


def ledger_for(facts: FixtureFacts, at: datetime) -> LedgerState:
    ledger = FinancialLedger()
    result = ledger.initial()
    for fact in facts.facts:
        if fact.received_at <= at and isinstance(fact.payload, FillEvidence):
            result = ledger.receive(result, fact, at=fact.received_at)
            assert not result.conflicts
    return result


@contextmanager
def component(
    folder: Path,
    facts: FixtureFacts = EMPTY_FACTS,
    *,
    rate: str = "0.0001",
    price: str | None = "100",
    finality: bool = True,
    has_inventory: bool = True,
    received: datetime = RECEIVED,
    inventory_received: datetime = END,
    mode: VerificationMode = VerificationMode.STRICT_FULL,
    exact_reference: bool = True,
) -> Iterator[tuple[DynamicFunding, FundingState]]:
    inv = (
        inventory(2, start=START, end=END, available=inventory_received) if has_inventory else None
    )
    path, ids = registered(
        folder,
        registration(
            folder,
            [row(2, rate=rate, price=price, available=received, start=START)],
            inv,
            start=START,
            end=END,
        ),
    )
    with prepared(path, ids, inv, mode, start=START, end=END, cutoff=max(END, received)) as history:
        declarations = None
        if finality:
            fill_ids = tuple(
                f.payload.fill.fill_id
                for f in facts.facts
                if isinstance(f.payload, FillEvidence) and f.payload.fill.timestamp <= FUNDING_AT
            )
            declarations = FixtureFinalitySource(
                (
                    FixtureExposureFinality(
                        FUNDING_AT,
                        RECEIVED,
                        facts.identity,
                        fill_ids,
                        "Independently declared complete fixture manifest",
                        "SYNTHETIC_FIXTURE_ONLY",
                    ),
                )
            )
        calendars = FixtureCalendarSource(
            (
                FixtureFundingCalendar(
                    START,
                    END,
                    (FUNDING_AT,),
                    END,
                    history.evidence.economic_identity,
                    "Explicit closed endpoint has no occurrence",
                    "SYNTHETIC_FIXTURE_ONLY",
                ),
            )
        )
        dynamic = DynamicFunding(
            HistoricalFundingSource(history, publication()),
            dynamic_policy(
                reference_semantics=("FIXTURE_MARK_AT_FUNDING_TIME" if exact_reference else None)
            ),
            declarations,
            calendars,
        )
        yield dynamic, dynamic.initial(START, END, facts, None, FinancialLedger().identity)


def advance(
    dynamic: DynamicFunding, state: FundingState, facts: FixtureFacts, at: datetime
) -> FundingState:
    context = FundingContext(
        at, START, END, ledger_for(facts, at), facts.identity, at + timedelta(minutes=1)
    )
    return dynamic.propose(state, context)[0]


def performance_value(state: FundingState, facts: FixtureFacts) -> Decimal | None:
    p = FinancialPerformance(policy(capital=None))
    ps = p.observe(p.initial(START, END), FinancialObservation(END, ledger_for(facts, END)))
    for event in state.events:
        ps = p.observe(ps, FinancialObservation(END, event))
    assert not ps.conflicts
    p.validate(ps, END, ledger_for(facts, END))
    value = p.value(ps, END)
    assert value.equity.value is None
    if facts.facts:
        assert value.fees.value is None
    if FinancialLedger().report(ledger_for(facts, END)).open_quantity > 0:
        assert value.unrealized.value is None
    return value.funding.value


@pytest.mark.parametrize("rate,amount", [(".0001", "-.10"), ("-.0001", ".10"), ("0", "0")])
def test_a_b_confirmed_long_sign_once_and_idempotence(
    tmp_path: Path, rate: str, amount: str
) -> None:
    facts = FixtureFacts((buy(),))
    with component(tmp_path, facts, rate=rate) as (dynamic, state):
        state = advance(dynamic, state, facts, END)
        assert state.latest[0].status == "ASSIGNED"
        assert state.latest[0].quantity == 10
        assert performance_value(state, facts) == Decimal(amount)
        assert advance(dynamic, state, facts, END) == state
        evidence = state.latest[0].payment
        assert evidence is not None and isinstance(evidence.payload, FundingEvidence)
        assert evidence.payload.effective_at == FUNDING_AT and evidence.payload.amount_usdc is None
        assert evidence.payload.reference.provenance.value == "MODELED"


@pytest.mark.parametrize(
    "case,trades,expected,status",
    [
        ("C", (), "0", "NO_EXPOSURE"),
        ("D", (buy(),), "10", "ASSIGNED"),
        ("E", (buy(second=121),), "0", "NO_EXPOSURE"),
        ("F", (buy(second=120),), None, "BLOCKED"),
        ("K_Q", (buy(), buy(2, second=60, side=Side.SELL)), "0", "NO_EXPOSURE"),
        ("L", (buy(), buy(2, second=140, side=Side.SELL)), "10", "ASSIGNED"),
        (
            "M",
            (buy(), buy(2, second=140, side=Side.SELL), buy(3, second=160, round_number=200)),
            "10",
            "ASSIGNED",
        ),
        (
            "N",
            (
                buy(quantity="1"),
                buy(2, second=50, quantity="2"),
                buy(3, second=70, quantity="3"),
                buy(4, second=90, quantity="4"),
            ),
            "10",
            "ASSIGNED",
        ),
        ("O", (buy(quantity="1"),), "1", "ASSIGNED"),
        ("P", (buy(), buy(2, second=60, side=Side.SELL, quantity="4")), "6", "ASSIGNED"),
    ],
)
def test_c_to_q_actual_exposure_round_and_tie(
    tmp_path: Path, case: str, trades: tuple[ReplayFact, ...], expected: str | None, status: str
) -> None:
    facts = FixtureFacts(trades)
    with component(tmp_path, facts) as (dynamic, state):
        state = advance(dynamic, state, facts, END)
        decision = state.latest[0]
        assert decision.status == status, case
        assert decision.quantity == (None if expected is None else Decimal(expected))
        if status == "ASSIGNED":
            assert decision.round_id == RoundId(UUID(int=100))
        else:
            assert decision.payment is None
        assert advance(dynamic, state, facts, END) == state
        assert performance_value(state, facts) == (
            None if status == "BLOCKED" else -Decimal(expected or "0") * Decimal(".01")
        )


@pytest.mark.parametrize("price,contract", [(None, True), ("100", False)])
def test_g_reference_never_falls_back(tmp_path: Path, price: str | None, contract: bool) -> None:
    facts = FixtureFacts((buy(),))
    with component(tmp_path, facts, price=price, exact_reference=contract) as (dynamic, state):
        state = advance(dynamic, state, facts, END)
        assert state.latest[0].reason == "REFERENCE_NOT_AVAILABLE"
        assert state.latest[0].status == "FINALIZED_EXPOSURE"
        assert not state.events and performance_value(state, facts) is None


@pytest.mark.parametrize("has_finality,has_inventory", [(False, True), (True, False)])
def test_j_clock_and_empty_read_are_not_completeness(
    tmp_path: Path, has_finality: bool, has_inventory: bool
) -> None:
    with component(tmp_path, finality=has_finality, has_inventory=has_inventory) as (
        dynamic,
        state,
    ):
        state = advance(dynamic, state, FixtureFacts(), END)
        assert not state.coverage
        if not has_finality:
            assert state.latest[0].status == "PENDING_EXPOSURE" and state.pending
        assert performance_value(state, FixtureFacts()) is None


@pytest.mark.parametrize("assigned", [False, True])
def test_r_s_late_unknown_fill_cannot_change_finalized_prefix(
    tmp_path: Path, assigned: bool
) -> None:
    facts = FixtureFacts((buy(),) if assigned else ())
    with component(tmp_path, facts) as (dynamic, state):
        state = advance(dynamic, state, facts, RECEIVED)
        before = state
        late = buy(99, second=100, received=END)
        with pytest.raises(ValueError, match="LATE_FILL_CONTRADICTS_FINALITY"):
            dynamic.validate_fact(state, late, ledger_for(facts, RECEIVED))
        assert state == before
        if assigned:
            dynamic.validate_fact(state, facts.facts[0], ledger_for(facts, RECEIVED))


def test_t_inventory_late_no_retroactive_coverage(tmp_path: Path) -> None:
    facts = FixtureFacts((buy(),))
    with component(tmp_path, facts) as (dynamic, state):
        state = advance(dynamic, state, facts, RECEIVED)
        assert state.latest[0].status == "ASSIGNED" and not state.coverage
        prefix = state.journal
        state = advance(dynamic, state, facts, END)
        assert state.inventory is not None and state.coverage
        assert state.journal == prefix
        assert state.coverage[0].received_at == END


def test_ad_receipt_after_request_end_stays_unconsumed(tmp_path: Path) -> None:
    facts = FixtureFacts((buy(),))
    with component(tmp_path, facts, received=END + timedelta(minutes=1)) as (dynamic, state):
        state = advance(dynamic, state, facts, END)
        assert state.cursor == 0 and not state.events and not state.journal
        assert not state.coverage


def test_z_verification_modes_share_economic_ids(tmp_path: Path) -> None:
    facts = FixtureFacts((buy(),))
    # Same original registry, bytes and declarations, independently prepared in both modes.
    inv = inventory(2, start=START, end=END)
    path, ids = registered(
        tmp_path, registration(tmp_path, [row(2, start=START)], inv, start=START, end=END)
    )
    results = []
    for mode in VerificationMode:
        with prepared(path, ids, inv, mode, start=START, end=END) as history:
            finality = (
                FixtureFinalitySource(
                    (
                        FixtureExposureFinality(
                            FUNDING_AT,
                            RECEIVED,
                            facts.identity,
                            (facts.facts[0].payload.fill.fill_id,),
                            "Complete declared fixture",
                            "SYNTHETIC_FIXTURE_ONLY",
                        ),
                    )
                )
                if isinstance(facts.facts[0].payload, FillEvidence)
                else None
            )
            dynamic = DynamicFunding(
                HistoricalFundingSource(history, publication()), dynamic_policy(), finality
            )
            state = dynamic.initial(START, END, facts, None, FinancialLedger().identity)
            results.append(advance(dynamic, state, facts, END))
    assert results[0].identity != results[1].identity
    assert results[0].economic_identity == results[1].economic_identity
    assert results[0].events == results[1].events


@pytest.mark.parametrize(
    "field,value",
    [
        ("publication", "OBSERVED"),
        ("max_rates", 513),
        ("max_journal_entries", 2049),
        ("max_fills", 4097),
        ("max_window_minutes", 44641),
        ("reference_semantics", "TRADE_CLOSE"),
    ],
)
def test_authorization_and_resource_limits(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        dynamic_policy(**{field: value})


def test_u_inclusive_endpoint_requires_explicit_rate_not_half_open_absence(tmp_path: Path) -> None:
    end = END + timedelta(minutes=2)
    history_end = END + timedelta(minutes=1)
    offset = int((END - START).total_seconds() // 60)
    inv = inventory(offset, start=START, end=history_end)
    path, ids = registered(
        tmp_path,
        registration(tmp_path, [row(offset, start=START)], inv, start=START, end=history_end),
    )
    facts = FixtureFacts((buy(),))
    with prepared(
        path, ids, inv, VerificationMode.STRICT_FULL, start=START, end=history_end, cutoff=end
    ) as history:
        fact = facts.facts[0].payload
        assert isinstance(fact, FillEvidence)
        finality = FixtureFinalitySource(
            (
                FixtureExposureFinality(
                    END,
                    END,
                    facts.identity,
                    (fact.fill.fill_id,),
                    "Complete fixture executions through end",
                    "SYNTHETIC_FIXTURE_ONLY",
                ),
            )
        )
        calendars = FixtureCalendarSource(
            (
                FixtureFundingCalendar(
                    START,
                    END,
                    (END,),
                    END,
                    history.evidence.economic_identity,
                    "Explicit occurrence AT inclusive end",
                    "SYNTHETIC_FIXTURE_ONLY",
                ),
            )
        )
        dynamic = DynamicFunding(
            HistoricalFundingSource(history, publication()), dynamic_policy(), finality, calendars
        )
        initial = dynamic.initial(START, end, facts, None, FinancialLedger().identity)
        context = FundingContext(
            END, START, end, ledger_for(facts, END), facts.identity, END + timedelta(minutes=1)
        )
        state, _ = dynamic.propose(initial, context)
        assert state.latest[0].payment is not None and not state.coverage
        assert state.latest[0].payment.payload.effective_at == END
        state, _ = dynamic.propose(state, replace(context, at=end))
        assert len(state.coverage) == 1
        payload = state.coverage[0].payload
        payment = state.latest[0].payment
        assert payment is not None and isinstance(payload, CoverageEvidence)
        assert payload.event_ids == (payment.payload.event_id,)
        assert payload.end == END


@pytest.mark.parametrize("contradictory", [False, True])
def test_h_i_original_source_deduplication_and_late_conflict(
    tmp_path: Path, contradictory: bool
) -> None:
    inv = inventory(2, start=START, end=END)
    first = registration(tmp_path, [row(2, start=START)], inv, start=START, end=END, name="first")
    second = registration(
        tmp_path,
        [
            row(
                2,
                start=START,
                rate="-.0001" if contradictory else "0.0001",
                available=RECEIVED if contradictory else FUNDING_AT,
            )
        ],
        inv,
        start=START,
        end=END,
        name="second",
    )
    path, ids = registered(tmp_path, first, second)
    facts = FixtureFacts()
    if contradictory:
        # Existing adapter rejects contradictory prepared intervals BEFORE constructing a runner.
        with pytest.raises(ValueError, match="CONFLICTED"):
            with prepared(path, ids, inv, VerificationMode.STRICT_FULL, start=START, end=END):
                pass
        return
    with prepared(path, ids, inv, VerificationMode.STRICT_FULL, start=START, end=END) as history:
        dynamic = DynamicFunding(HistoricalFundingSource(history, publication()), dynamic_policy())
        state = dynamic.initial(START, END, facts, None, FinancialLedger().identity)
        state = advance(dynamic, state, facts, FUNDING_AT)
        state = advance(dynamic, state, facts, END)
        assert state.cursor == 1 and len(state.journal) == 1


def test_ae_later_inventory_calendar_conflict_retains_prefix(tmp_path: Path) -> None:
    facts = FixtureFacts((buy(),))
    with component(tmp_path, facts) as (dynamic, state):
        original = dynamic.calendars
        assert original is not None
        contradictory = replace(original.events[0], expected_timestamps=())
        declared = replace(
            dynamic, calendars=FixtureCalendarSource((original.events[0], contradictory))
        )
        state = declared.initial(START, END, facts, None, FinancialLedger().identity)
        state = advance(declared, state, facts, RECEIVED)
        prefix = state
        with pytest.raises(ValueError, match="CONTRADICTS|CONTRADICTION"):
            advance(declared, state, facts, END)
        assert state == prefix and state.latest[0].status == "ASSIGNED"


def test_manifest_missing_fill_waits_until_actual_delivery(tmp_path: Path) -> None:
    facts = FixtureFacts((buy(received=END),))
    with component(tmp_path, facts) as (dynamic, state):
        state = advance(dynamic, state, facts, RECEIVED)
        assert state.latest[0].status == "PENDING_EXPOSURE"
        state = advance(dynamic, state, facts, END)
        decision = state.latest[0]
        assert decision.status == "ASSIGNED" and decision.payment is not None
        assert decision.payment.received_at == END
        assert decision.finalization is not None and decision.finalization.available_at == END
        assert len(state.journal) == 2
        assert advance(dynamic, state, facts, END) == state


@pytest.mark.parametrize("fault", ["prepared_close", "session_close", "source_changed", "real_ttl"])
def test_trust_loss_during_payment_construction_has_no_proposed_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from time import monotonic, sleep

    from trading_engine.replay.dynamic_funding import core
    from trading_engine.replay.historical_validation.funding_history import PreparedFundingHistory
    from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
    from trading_engine.replay.historical_validation.verification import SessionPolicy

    inv = inventory(2, start=START, end=END)
    reg = registration(tmp_path, [row(2, start=START)], inv, start=START, end=END)
    path, ids = registered(tmp_path, reg)
    facts = FixtureFacts((buy(),))
    with SealedSegmentSession(
        path,
        ids,
        session_identity="dynamic-adversarial",
        opened_at=END,
        policy=SessionPolicy(
            1 if fault == "real_ttl" else 3600,
            10000,
            "Synthetic trust test, production validators intact",
        ),
    ) as session:
        with PreparedFundingHistory(
            path,
            ids,
            START,
            END,
            cutoff=END,
            inventory=inv,
            mode=VerificationMode.SEALED_SEGMENT_SESSION,
            session=session,
        ) as history:
            fact = facts.facts[0].payload
            assert isinstance(fact, FillEvidence)
            finality = FixtureFinalitySource(
                (
                    FixtureExposureFinality(
                        FUNDING_AT,
                        RECEIVED,
                        facts.identity,
                        (fact.fill.fill_id,),
                        "Complete fixture manifest",
                        "SYNTHETIC_FIXTURE_ONLY",
                    ),
                )
            )
            dynamic = DynamicFunding(
                HistoricalFundingSource(history, publication()), dynamic_policy(), finality
            )
            state = dynamic.initial(START, END, facts, None, FinancialLedger().identity)
            state = advance(dynamic, state, facts, START)
            prefix = state
            original = FundingEvidence

            def constructing(*args: Any, **kwargs: Any) -> FundingEvidence:
                result = original(*args, **kwargs)
                if fault == "prepared_close":
                    history.close()
                elif fault == "session_close":
                    session.close()
                elif fault == "source_changed":
                    with Path(reg.original_file).open("a") as stream:
                        stream.write("\n")
                else:
                    remaining = session._started + session.policy.max_seconds - monotonic()
                    assert remaining > 0
                    sleep(remaining + 0.02)
                return result

            monkeypatch.setattr(core, "FundingEvidence", constructing)
            error = {
                "prepared_close": "PREPARED_FUNDING_NOT_AVAILABLE",
                "session_close": "VERIFICATION_NOT_AVAILABLE",
                "source_changed": "SOURCE_CHANGED",
                "real_ttl": "SESSION_EXPIRED",
            }[fault]
            with pytest.raises(ValueError, match=error):
                advance(dynamic, state, facts, END)
            assert state == prefix and not state.events


def test_y_real_history_cannot_be_laundered_as_modeled(tmp_path: Path) -> None:
    from trading_engine.replay.historical_validation.segments import RegistryProvenance

    inv = inventory(2, start=START, end=END)
    reg = registration(
        tmp_path,
        [row(2, start=START)],
        inv,
        start=START,
        end=END,
        provenance=RegistryProvenance.DECLARED,
    )
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, inv, VerificationMode.STRICT_FULL, start=START, end=END) as history:
        with pytest.raises(ValueError, match="PUBLICATION_NOT_AUTHORIZED"):
            DynamicFunding(HistoricalFundingSource(history, publication()), dynamic_policy())


def test_later_contradictory_finality_rejected_even_without_pending_rate(tmp_path: Path) -> None:
    facts = FixtureFacts((buy(),))
    with component(tmp_path, facts) as (dynamic, _):
        assert dynamic.finality is not None
        first = dynamic.finality.events[0]
        later = replace(first, received_at=END, fill_ids=())
        dynamic = replace(dynamic, finality=FixtureFinalitySource((first, later)))
        state = dynamic.initial(START, END, facts, None, FinancialLedger().identity)
        state = advance(dynamic, state, facts, RECEIVED)
        prefix = state
        assert not state.pending and state.latest[0].status == "ASSIGNED"
        with pytest.raises(ValueError, match="UNDECLARED_FILLS|MANIFEST_CONTRADICTION"):
            advance(dynamic, state, facts, END)
        assert state == prefix
