"""Ecuaciones y contratos offline; fixtures sintéticos, sin resultados históricos."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from trading_engine.models.enums import Side
from trading_engine.models.ids import FillId, OrderId, RoundId
from trading_engine.models.values import Symbol
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_performance import FinancialPerformance, PerformancePolicy
from trading_engine.replay.financial_performance.evidence import (
    CoverageEvidence,
    FeeEvidence,
    FinancialEvent,
    FinancialPayload,
    FixtureFinancialSource,
    FixtureMarks,
    FundingEvidence,
    MarkEvidence,
    Origin,
    Reference,
    TransferEvidence,
)
from trading_engine.replay.financial_performance.models import (
    FinancialObservation,
    PerformanceState,
)
from trading_engine.replay.financial_performance.policy import CapitalEvidence, FeeRatePolicy
from trading_engine.replay.ports import ReplayFact

from .conftest import DUE, END, START, SYMBOL
from .test_financial_ledger import apply, execution

FIXTURE = Reference(
    "manual-fixture", "synthetic-accounting-v1", Origin.FIXTURE, "Synthetic equations only"
)
MODELED = Reference(
    "assumed-rates",
    "synthetic-accounting-v1",
    Origin.MODELED,
    "Sensitivity only; not Binance rates",
)
AT = DUE + timedelta(seconds=4)
RID = RoundId(UUID(int=100))


def policy(
    *, capital: str | None = "1000", rates: FeeRatePolicy | None = None
) -> PerformancePolicy:
    return PerformancePolicy(
        "BASE",
        "Synthetic fixture; no calibrated market",
        None if capital is None else CapitalEvidence(Decimal(capital), START, FIXTURE),
        rates,
    )


def event(
    payload: FeeEvidence | FundingEvidence | TransferEvidence | MarkEvidence | CoverageEvidence,
    *,
    at: datetime = AT,
    sequence: int = 1,
) -> FinancialEvent:
    return FinancialEvent(at, sequence, payload)


def mark(price: str = "95", *, at: datetime = AT, lifetime: int = 60000) -> MarkEvidence:
    return MarkEvidence(
        "mark:" + at.isoformat(), at, SYMBOL, Decimal(price), lifetime, True, FIXTURE
    )


def coverage(
    name: str, *, at: datetime = AT, start: datetime = START, ids: tuple[str, ...] = ()
) -> CoverageEvidence:
    return CoverageEvidence(name + ":" + at.isoformat(), at, start, at, name, ids, FIXTURE)


def fees(ledger: LedgerState, amounts: tuple[str, ...] | None = None) -> tuple[FeeEvidence, ...]:
    return tuple(
        FeeEvidence(
            "fee:" + str(e.fill_id),
            e.fill_id,
            e.order_id,
            e.executed_at,
            Decimal("0" if amounts is None else amounts[i]),
            "USDC",
            FIXTURE,
        )
        for i, e in enumerate(ledger.entries)
    )


def state_for(
    performance: FinancialPerformance,
    ledger: LedgerState,
    *events: FinancialEvent,
    at: datetime = AT,
) -> PerformanceState:
    state = performance.observe(performance.initial(START, END), FinancialObservation(at, ledger))
    for value in events:
        state = performance.observe(state, FinancialObservation(value.received_at, value))
    return state


def complete(
    performance: FinancialPerformance,
    ledger: LedgerState,
    *,
    price: str = "95",
    amounts: tuple[str, ...] | None = None,
    funding: FundingEvidence | None = None,
    at: datetime = AT,
) -> PerformanceState:
    events = [event(f, at=at) for f in fees(ledger, amounts)]
    if funding is not None:
        events.append(event(funding, at=at))
    events += [
        event(
            coverage("FUNDING", at=at, ids=() if funding is None else (funding.event_id,)), at=at
        ),
        event(coverage("TRANSFERS", at=at), at=at),
        event(mark(price, at=at), at=at),
    ]
    state = state_for(performance, ledger, *events, at=at)
    assert not state.conflicts
    return performance.observe(state, FinancialObservation(at, "VALUE"))


@pytest.mark.parametrize("partial", [False, True])
def test_required_numeric_cases_a_and_b(partial: bool) -> None:
    ledger = FinancialLedger()
    facts: tuple[ReplayFact, ...] = (execution(1, Side.BUY, "10", "100"),)
    if partial:
        facts += (execution(2, Side.SELL, "5", "110"),)
    position = apply(ledger, *facts)
    performance = FinancialPerformance(policy())
    state = complete(performance, position)
    value = performance.report(state, AT).current
    assert value.unrealized.value == Decimal("-25" if partial else "-50")
    assert value.gross_realized.value == Decimal("50" if partial else "0")
    assert position.round_ledger[0].open_quantity == Decimal("5" if partial else "10")
    assert position.round_ledger[0].open_cost_basis_usdc == Decimal("500" if partial else "1000")
    assert value.rounds[0].net_realized_closed.value is None
    assert value.rounds[0].net_cash.value == value.gross_realized.value
    performance.validate(state, AT, position)


def test_required_case_c_wallet_equity_and_net_cash() -> None:
    ledger = apply(
        FinancialLedger(), execution(1, Side.BUY, "10", "100"), execution(2, Side.SELL, "5", "110")
    )
    funding = FundingEvidence(
        "funding-c",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("5"),
        FIXTURE,
        amount_usdc=Decimal("-3"),
    )
    performance = FinancialPerformance(policy())
    state = complete(performance, ledger, amounts=("1", "1.10"), funding=funding)
    current = performance.report(state, AT).current
    assert current.fees.value == Decimal("2.10")
    assert current.funding.value == Decimal("-3")
    assert current.wallet.value == Decimal("1044.90")
    assert current.equity.value == Decimal("1019.90")
    assert current.net_cash.value == Decimal("44.90")
    assert current.unrealized.value == Decimal("-25")
    assert current.rounds[0].funding.value == Decimal("-3")
    assert current.rounds[0].net_realized_closed.value is None


def test_required_case_d_drawdown_includes_unrealized_losses() -> None:
    p = FinancialPerformance(policy())
    ledger = FinancialLedger()
    empty = ledger.initial()
    state = state_for(p, empty, at=START)
    state = p.observe(state, FinancialObservation(START, "VALUE"))
    assert state.series[0].equity.value == 1000
    bought = apply(ledger, execution(1, Side.BUY, "10", "100"))
    first = complete(p, bought, price="110")
    # Incorporar el prefijo inicial y reproducir el mismo conjunto de observaciones.
    for observation in first.journal:
        state = p.observe(state, observation)
    later = AT + timedelta(seconds=1)
    for payload in (
        coverage("FUNDING", at=later),
        coverage("TRANSFERS", at=later),
        mark("99", at=later),
    ):
        state = p.observe(state, FinancialObservation(later, event(payload, at=later)))
    state = p.observe(state, FinancialObservation(later, "VALUE"))
    report = p.report(state, later)
    assert not state.conflicts
    assert report.current.equity.value == 990
    assert report.peak_equity == 1100 and report.peak_at == AT
    assert report.max_drawdown_usdc.value == report.drawdown_usdc.value == 110
    assert report.max_drawdown_pct.value == report.drawdown_pct.value == 10
    assert report.max_peak_at == AT and report.max_valley_at == later


@pytest.mark.parametrize("missing", ["fees", "funding", "mark", "capital", "transfers"])
def test_missing_evidence_never_becomes_zero(missing: str) -> None:
    p = FinancialPerformance(policy(capital=None if missing == "capital" else "1000"))
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    events = [] if missing == "fees" else [event(f) for f in fees(ledger)]
    if missing != "funding":
        events.append(event(coverage("FUNDING")))
    if missing != "transfers":
        events.append(event(coverage("TRANSFERS")))
    if missing != "mark":
        events.append(event(mark()))
    state = state_for(p, ledger, *events)
    value = p.value(state, AT)
    assert not state.conflicts and value.gross_realized.value == 0
    assert value.equity.value is None
    if missing in {"fees", "funding"}:
        assert getattr(value, missing).value is None and value.net_cash.value is None
    if missing == "mark":
        assert value.unrealized.value is None
    if missing in {"capital", "transfers"}:
        assert value.wallet.value is None


def test_closed_position_requires_costs_but_not_mark() -> None:
    ledger = apply(
        FinancialLedger(), execution(1, Side.BUY, "10", "100"), execution(2, Side.SELL, "10", "101")
    )
    p = FinancialPerformance(policy())
    state = state_for(p, ledger)
    assert p.value(state, AT).unrealized.value == 0
    assert p.value(state, AT).net_cash.value is None
    assert p.value(state, AT).rounds[0].net_realized_closed.value is None
    ledger = FinancialLedger().reconcile_closed(ledger, (RID,), at=AT)
    state = complete(p, ledger, amounts=("1", "1"))
    assert p.value(state, AT).rounds[0].net_realized_closed.value == 8
    assert p.value(state, AT).equity.value == p.value(state, AT).wallet.value == 1008


@pytest.mark.parametrize("amount", ["3", "-3", "0"])
def test_explicit_funding_signs(amount: str) -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    f = FundingEvidence(
        "funding",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("10"),
        FIXTURE,
        amount_usdc=Decimal(amount),
    )
    p = FinancialPerformance(policy())
    state = complete(p, ledger, funding=f)
    assert p.value(state, AT).funding.value == Decimal(amount)


@pytest.mark.parametrize("rate,amount", [("0.001", "-1"), ("-0.001", "1"), ("0", "0")])
def test_modeled_funding_uses_historical_quantity_once(rate: str, amount: str) -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    f = FundingEvidence(
        "modeled-funding",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("10"),
        MODELED,
        rate=Decimal(rate),
        reference_price=Decimal("100"),
        reference_price_at=AT - timedelta(seconds=1),
        positive_rate_pays=True,
    )
    p = FinancialPerformance(policy())
    state = complete(p, ledger, funding=f)
    assert p.value(state, AT).funding.value == Decimal(amount)
    assert p.value(state, AT).equity.status == "MODELED"


@pytest.mark.parametrize("role,expected", [("MAKER", "1"), ("TAKER", "2")])
def test_fee_rate_requires_explicit_fill_role_not_order_type(role: str, expected: str) -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    rates = FeeRatePolicy(Decimal(".001"), Decimal(".002"), ((FillId(UUID(int=1)), role),), MODELED)
    p = FinancialPerformance(policy(rates=rates))
    state = state_for(p, ledger)
    assert p.value(state, AT).fees.value == Decimal(expected)
    assert p.value(state, AT).fees.status == "MODELED"
    with pytest.raises(ValueError):
        FeeRatePolicy(
            Decimal(".001"), Decimal(".002"), ((FillId(UUID(int=1)), "AUTO_MARKET"),), MODELED
        )


def test_policy_without_fill_role_leaves_fee_missing() -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    rates = FeeRatePolicy(Decimal(".001"), Decimal(".002"), (), MODELED)
    p = FinancialPerformance(policy(rates=rates))
    assert p.value(state_for(p, ledger), AT).fees.value is None


@pytest.mark.parametrize("kind", ["fee", "funding", "transfer", "mark", "coverage"])
def test_duplicates_idempotent_and_conflicts_block_without_overwrite(kind: str) -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    payloads: dict[str, FinancialPayload] = {
        "fee": fees(ledger)[0],
        "funding": FundingEvidence(
            "fund",
            SYMBOL,
            AT - timedelta(seconds=1),
            RID,
            Decimal("10"),
            FIXTURE,
            amount_usdc=Decimal("1"),
        ),
        "transfer": TransferEvidence("deposit", AT, Decimal("3"), FIXTURE, "DEPOSIT"),
        "mark": mark(),
        "coverage": coverage("TRANSFERS"),
    }
    payload = payloads[kind]
    p = FinancialPerformance(policy())
    value = event(payload)
    state = state_for(p, ledger, value)
    duplicate = replace(value, received_at=AT + timedelta(seconds=1), sequence=2)
    assert p.observe(state, FinancialObservation(duplicate.received_at, duplicate)) == state
    altered = replace(payload, reference=replace(FIXTURE, source="contradictory"))
    conflict = p.observe(state, FinancialObservation(AT, event(altered)))
    assert conflict.conflicts and conflict.events == state.events
    assert p.report(conflict, AT).current.equity.status == "BLOCKED"
    p.validate(conflict, AT, ledger)


@pytest.mark.parametrize("wrong", ["fill", "order", "time", "double", "modeled_double"])
def test_wrong_fee_binding_or_double_cost_blocked(wrong: str) -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    fee = fees(ledger)[0]
    rates = (
        FeeRatePolicy(Decimal("0"), Decimal("0"), ((fee.fill_id, "MAKER"),), MODELED)
        if wrong == "modeled_double"
        else None
    )
    p = FinancialPerformance(policy(rates=rates))
    changes: dict[str, object] = {}
    if wrong == "fill":
        fee = replace(fee, fill_id=FillId(UUID(int=2)))
    elif wrong == "order":
        fee = replace(fee, order_id=OrderId(UUID(int=2)))
    elif wrong == "time":
        fee = replace(fee, effective_at=START)
    state = state_for(p, ledger, event(fee))
    if wrong == "double":
        state = p.observe(
            state, FinancialObservation(AT, event(replace(fee, event_id="second-fee")))
        )
    assert state.conflicts
    assert not changes


def test_fee_other_currency_is_unavailable_without_conversion() -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    state = state_for(p, ledger, event(replace(fees(ledger)[0], currency="BNB")))
    assert not state.conflicts
    assert p.value(state, AT).fees.value is None
    assert p.value(state, AT).net_cash.value is None


@pytest.mark.parametrize("bad", [1.0, True, "0.001", 1, Decimal("NaN"), Decimal("Infinity")])
def test_bad_decimal_rate_or_amount_rejected(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        FeeRatePolicy(bad, Decimal("0"), (), MODELED)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        CapitalEvidence(bad, START, FIXTURE)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        replace(mark(), price=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        "wrong_quantity",
        "zero_position",
        "future_quantity",
        "same_time",
        "wrong_round",
        "wrong_symbol",
    ],
)
def test_funding_exposure_conflicts(bad: str) -> None:
    ledger = apply(
        FinancialLedger(), execution(1, Side.BUY, "10", "100"), execution(2, Side.SELL, "5", "110")
    )
    f = FundingEvidence(
        "fund",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("5"),
        FIXTURE,
        amount_usdc=Decimal("-1"),
    )
    if bad == "wrong_quantity":
        f = replace(f, exposure_quantity=Decimal("10"))
    elif bad == "zero_position":
        f = replace(f, effective_at=START, exposure_quantity=Decimal("0"))
    elif bad == "future_quantity":
        f = replace(f, effective_at=DUE + timedelta(milliseconds=1500))
    elif bad == "same_time":
        f = replace(f, effective_at=DUE + timedelta(seconds=2))
    elif bad == "wrong_round":
        f = replace(f, round_id=RoundId(UUID(int=200)))
    else:
        f = replace(f, symbol=Symbol("SHIBUSDC"))
    p = FinancialPerformance(policy())
    assert state_for(p, ledger, event(f)).conflicts


def test_late_fill_invalidates_previous_funding_exposure() -> None:
    ledger = FinancialLedger()
    bought = apply(ledger, execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    fund = FundingEvidence(
        "fund",
        SYMBOL,
        DUE + timedelta(seconds=3),
        RID,
        Decimal("10"),
        FIXTURE,
        amount_usdc=Decimal("-1"),
    )
    state = state_for(p, bought, event(fund))
    late = execution(2, Side.SELL, "5", "110", received_at=AT + timedelta(seconds=1))
    updated = ledger.receive(bought, late, at=late.received_at)
    state = p.observe(state, FinancialObservation(late.received_at, updated))
    assert state.conflicts and "Fill tardío" in state.conflicts[0].reason
    p.validate(state, late.received_at, updated)


@pytest.mark.parametrize("invalid", ["future", "stale", "close", "continuity", "late", "symbol"])
def test_mark_temporal_contract(invalid: str) -> None:
    p = FinancialPerformance(policy())
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    if invalid == "future":
        with pytest.raises(ValueError):
            event(mark(at=AT + timedelta(seconds=1)))
        return
    if invalid == "close":
        with pytest.raises(ValueError):
            replace(mark(), price_kind="TRADES_CLOSE")
        return
    m = mark(lifetime=0) if invalid == "stale" else mark()
    if invalid == "continuity":
        m = replace(m, continuous=False)
    if invalid == "symbol":
        m = replace(m, symbol=Symbol("SHIBUSDC"))
    state = state_for(p, ledger, event(m))
    if invalid == "late":
        state = p.observe(
            state, FinancialObservation(AT, event(replace(m, event_id="late", effective_at=DUE)))
        )
    if invalid in {"late", "symbol"}:
        assert state.conflicts
    else:
        assert p.value(state, AT + timedelta(milliseconds=1)).unrealized.value is None


def test_delayed_fee_updates_cash_on_receipt_without_rewriting_past() -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    state = state_for(
        p, ledger, event(coverage("FUNDING")), event(coverage("TRANSFERS")), event(mark())
    )
    state = p.observe(state, FinancialObservation(AT, "VALUE"))
    original = state.series
    later = AT + timedelta(seconds=1)
    state = p.observe(state, FinancialObservation(later, event(fees(ledger, ("1",))[0], at=later)))
    assert state.series == original
    assert p.value(state, later).fees.value == 1
    assert p.value(state, later).wallet.value is None  # cobertura solo hasta AT


@pytest.mark.parametrize("kind", ["DEPOSIT", "WITHDRAWAL"])
def test_external_flows_are_not_profit_and_negative_wallet_is_not_liquidation(kind: str) -> None:
    p = FinancialPerformance(policy(capital="10"))
    ledger = FinancialLedger().initial()
    transfer = TransferEvidence("movement", AT, Decimal("20"), FIXTURE, kind)
    state = state_for(
        p,
        ledger,
        event(transfer),
        event(coverage("TRANSFERS", ids=(transfer.event_id,))),
        event(coverage("FUNDING")),
    )
    state = p.observe(state, FinancialObservation(AT, "VALUE"))
    report = p.report(state, AT)
    assert report.current.wallet.value == Decimal("30" if kind == "DEPOSIT" else "-10")
    assert report.current.net_cash.value == 0
    assert report.current.wallet_negative == (kind == "WITHDRAWAL")
    assert report.general_return_pct == report.liquidation == "NOT_AVAILABLE"
    if kind == "WITHDRAWAL":
        assert report.drawdown_pct.value is None  # pico no positivo


def test_invalid_withdrawal_and_fake_observed_rejected() -> None:
    with pytest.raises(ValueError):
        TransferEvidence("withdraw", AT, Decimal("-1"), FIXTURE, "WITHDRAWAL")
    with pytest.raises(ValueError):
        replace(FIXTURE, provenance=Origin.OBSERVED)


def test_coverage_inventory_cannot_hide_event_or_future_missing_evidence() -> None:
    p = FinancialPerformance(policy())
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    promised = coverage("FUNDING", ids=("not-yet-received",))
    state = state_for(p, ledger, event(promised))
    assert not state.conflicts and p.value(state, AT).funding.value is None
    state = state_for(p, ledger, event(coverage("FUNDING")))
    f = FundingEvidence(
        "undeclared",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("10"),
        FIXTURE,
        amount_usdc=Decimal("1"),
    )
    state = p.observe(state, FinancialObservation(AT, event(f)))
    assert state.conflicts and "inventario" in state.conflicts[0].reason


def test_partial_coverage_and_unknown_origins_do_not_authorize_equity() -> None:
    p = FinancialPerformance(policy())
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    unknown = replace(FIXTURE, provenance=Origin.UNKNOWN)
    events = (
        event(replace(fees(ledger)[0], reference=unknown)),
        event(coverage("TRANSFERS")),
        event(coverage("FUNDING", start=AT - timedelta(seconds=1))),
        event(mark()),
    )
    state = state_for(p, ledger, *events)
    value = p.value(state, AT)
    assert value.fees.value is None and value.funding.value is None and value.equity.value is None


def test_incomplete_series_does_not_claim_full_period_maximum() -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    state = state_for(p, ledger)
    state = p.observe(state, FinancialObservation(AT, "VALUE"))
    later = AT + timedelta(seconds=1)
    for f in fees(ledger):
        state = p.observe(state, FinancialObservation(later, event(f, at=later)))
    for payload in (coverage("FUNDING", at=later), coverage("TRANSFERS", at=later), mark(at=later)):
        state = p.observe(state, FinancialObservation(later, event(payload, at=later)))
    state = p.observe(state, FinancialObservation(later, "VALUE"))
    report = p.report(state, later)
    assert report.valuation_coverage == "PARTIAL"
    assert report.max_drawdown_usdc.status == "INCOMPLETE"


@pytest.mark.parametrize("damage", ["journal", "wallet", "peak", "event", "policy", "identity"])
def test_snapshot_rederivation_rejects_tampering(damage: str) -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    state = complete(p, ledger)
    if damage == "journal":
        state = replace(state, journal=())
    elif damage == "wallet":
        state = replace(
            state,
            series=(
                replace(
                    state.series[0], wallet=replace(state.series[0].wallet, value=Decimal("999"))
                ),
            ),
        )
    elif damage == "peak":
        state = replace(state, peak_equity=Decimal("999"))
    elif damage == "event":
        state = replace(state, events=())
    elif damage == "policy":
        state = replace(state, policy=replace(state.policy, scenario="ADVERSO"))
    else:
        state = replace(state, identity="other")
    with pytest.raises(ValueError):
        p.validate(state, AT, ledger)


def test_decimal_context_independent_and_no_early_rounding() -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "3", "100.123456789"))
    p = FinancialPerformance(policy())
    expected = complete(p, ledger, price="95.123456789", amounts=("0.123456789",))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        actual = complete(p, ledger, price="95.123456789", amounts=("0.123456789",))
    assert actual == expected
    assert p.value(actual, AT).unrealized.value == Decimal("-15")
    assert p.value(actual, AT).fees.value == Decimal("0.123456789")


def test_sources_and_policy_identity_deterministic_no_default_capital() -> None:
    a, b = event(mark(), sequence=2), event(coverage("FUNDING"), sequence=1)
    assert FixtureFinancialSource((a, b)).identity == FixtureFinancialSource((b, a)).identity
    assert list(FixtureMarks((a,)).read(0, AT - timedelta(seconds=1))) == []
    assert list(FixtureMarks((a,)).read(1, AT)) == []
    with pytest.raises(ValueError):
        FixtureMarks((b,))
    with pytest.raises(ValueError):
        FixtureFinancialSource((a, a))
    p = PerformancePolicy("BASE", "No assumptions supplied as facts")
    assert p.capital is None and p.fees is None
    assert p.identity == replace(p).identity
    assert p.identity != replace(p, scenario="ADVERSO").identity
    with pytest.raises(ValueError):
        replace(p, rules_version="future")


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_rate",
        "float_rate",
        "wrong_price_time",
        "missing_sign_contract",
        "fixture_labeled_model",
        "mixed_modes",
    ],
)
def test_funding_model_requires_complete_price_time_and_sign_contract(invalid: str) -> None:
    f = FundingEvidence(
        "modeled",
        SYMBOL,
        AT,
        RID,
        Decimal("10"),
        MODELED,
        rate=Decimal(".001"),
        reference_price=Decimal("100"),
        reference_price_at=AT,
        positive_rate_pays=True,
    )
    with pytest.raises((ValueError, TypeError)):
        if invalid == "missing_rate":
            replace(f, rate=None)
        elif invalid == "float_rate":
            replace(f, rate=0.001)  # type: ignore[arg-type]
        elif invalid == "wrong_price_time":
            replace(f, reference_price_at=AT + timedelta(seconds=1))
        elif invalid == "missing_sign_contract":
            replace(f, positive_rate_pays=None)
        elif invalid == "fixture_labeled_model":
            replace(f, reference=FIXTURE)
        else:
            replace(f, amount_usdc=Decimal("1"))


def test_rereceiving_funding_under_another_id_does_not_charge_twice() -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    f = FundingEvidence(
        "funding-1",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("10"),
        FIXTURE,
        amount_usdc=Decimal("-1"),
    )
    state = state_for(p, ledger, event(f))
    state = p.observe(state, FinancialObservation(AT, event(replace(f, event_id="funding-2"))))
    assert state.conflicts and len(state.events) == 1


def test_round_costs_are_not_transferred_to_other_rounds() -> None:
    ledger = apply(
        FinancialLedger(),
        execution(1, Side.BUY, "10", "100", round_number=100),
        execution(2, Side.BUY, "5", "90", round_number=200),
    )
    p = FinancialPerformance(policy())
    f = FundingEvidence(
        "round-100-fund",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("10"),
        FIXTURE,
        amount_usdc=Decimal("-3"),
    )
    state = complete(p, ledger, funding=f, amounts=("1", "2"))
    first, second = p.value(state, AT).rounds
    assert first.funding.value == -3 and first.fees.value == 1
    assert second.funding.value == 0 and second.fees.value == 2
    assert first.net_cash.value == -4 and second.net_cash.value == -2


def test_no_future_event_processed_by_component() -> None:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    state = state_for(p, ledger)
    future = event(mark(at=AT + timedelta(seconds=1)), at=AT + timedelta(seconds=1))
    failed = p.observe(state, FinancialObservation(AT, future))
    assert failed.conflicts and failed.events == state.events
    assert failed.ledger == ledger


@pytest.mark.parametrize("extreme", ["overflow", "underflow"])
def test_fee_nonrepresentable_blocks_without_fake_zero(extreme: str) -> None:
    ledger = apply(
        FinancialLedger(),
        execution(1, Side.BUY, "1", "1e-999999999999999999" if extreme == "underflow" else "100"),
    )
    rate = Decimal("1e-999999999999999999" if extreme == "underflow" else "1e999999999999999999")
    p = FinancialPerformance(
        policy(rates=FeeRatePolicy(rate, rate, ((ledger.entries[0].fill_id, "TAKER"),), MODELED))
    )
    state = state_for(p, ledger)
    state = p.observe(state, FinancialObservation(AT, "VALUE"))
    assert state.conflicts
    assert p.report(state, AT).current.fees.status == "BLOCKED"
    p.validate(state, AT, ledger)
