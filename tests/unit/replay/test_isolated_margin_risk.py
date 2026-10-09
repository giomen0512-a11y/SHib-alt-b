"""Fixtures numéricos explícitos; no brackets ni balances reales de Binance."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from trading_engine.models.enums import Side
from trading_engine.models.ids import FillId
from trading_engine.models.values import Symbol
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import (
    FeeEvidence,
    FundingEvidence,
    MarkEvidence,
    Origin,
    Reference,
)
from trading_engine.replay.financial_performance.models import (
    PerformanceState,
)
from trading_engine.replay.isolated_margin_risk import IsolatedMarginRisk, MarginAllocationPolicy
from trading_engine.replay.isolated_margin_risk.evidence import (
    FixtureRiskSource,
    IsolatedMarginEvidence,
    MaintenanceBracket,
    MaintenanceBracketSchedule,
    MarginAdjustmentEvidence,
    RiskEvent,
    RiskStamp,
    RiskValuationEvidence,
)
from trading_engine.replay.isolated_margin_risk.models import RiskObservation, RiskState
from trading_engine.replay.isolated_margin_risk.solver import threshold

from .conftest import END, START, SYMBOL
from .test_financial_ledger import apply, execution
from .test_financial_performance import AT, FIXTURE, MODELED, RID, complete, policy


def margin_policy() -> MarginAllocationPolicy:
    return MarginAllocationPolicy(
        "EXECUTED_NOTIONAL_OVER_LEVERAGE",
        "PROPORTIONAL_INITIAL_ON_SELL",
        "RECONCILED_TO_ISOLATED",
        "OUTSIDE_ISOLATED",
        True,
        True,
        MODELED,
    )


def stamp(name: str, *, at: datetime = AT, sequence: int = 0) -> RiskStamp:
    return RiskStamp(name, at, at, sequence, FIXTURE, "synthetic-rules-v1")


def schedule(*, at: datetime = AT) -> MaintenanceBracketSchedule:
    return MaintenanceBracketSchedule(
        stamp("synthetic-schedule", at=at),
        START,
        END,
        (MaintenanceBracket(Decimal("0"), Decimal("1000000"), Decimal(".01"), Decimal("0")),),
    )


def numeric(
    price: str = "95",
    *,
    events: tuple[RiskEvent, ...] | None = None,
    allocation: MarginAllocationPolicy | None = None,
    ledger: LedgerState | None = None,
    amounts: tuple[str, ...] | None = None,
    funding: FundingEvidence | None = None,
    remove: str | None = None,
) -> tuple[IsolatedMarginRisk, RiskState, FinancialPerformance, PerformanceState]:
    raw = (
        apply(FinancialLedger(), execution(1, Side.BUY, "10", "100")) if ledger is None else ledger
    )
    p = FinancialPerformance(policy())
    full = complete(p, raw, price=price, amounts=amounts, funding=funding)
    if remove is not None:
        rebuilt = p.initial(START, END)
        for item in full.journal:
            payload = getattr(item.payload, "payload", None)
            if remove == "mark" and isinstance(payload, MarkEvidence):
                continue
            if remove == "fee" and isinstance(payload, FeeEvidence):
                continue
            if remove == "funding" and getattr(payload, "metric", None) == "FUNDING":
                continue
            rebuilt = p.observe(rebuilt, item)
        full = rebuilt
    source = FixtureRiskSource((schedule(),) if events is None else events)
    risk = IsolatedMarginRisk(margin_policy() if allocation is None else allocation, source)
    state = risk.initial(START, END)
    state = risk.observe(state, RiskObservation(AT, risk.context_from(p, full, AT)))
    for risk_event in source.events:
        state = risk.observe(state, RiskObservation(risk_event.stamp.received_at, risk_event))
    state = risk.observe(state, RiskObservation(AT, "VALUE"))
    assert not state.conflicts, state.conflicts
    return risk, state, p, full


@pytest.mark.parametrize(
    "price,condition,unrealized,balance,maintenance,buffer",
    [
        ("95", "ABOVE_MAINTENANCE", "-50", "200", "9.50", "190.50"),
        ("75", "BELOW_MAINTENANCE", "-250", "0", "7.50", "-7.50"),
        ("100", "ABOVE_MAINTENANCE", "0", "250", "10", "240"),
    ],
)
def test_numeric_a_b_c(
    price: str, condition: str, unrealized: str, balance: str, maintenance: str, buffer: str
) -> None:
    risk, state, p, financial = numeric(price)
    risk.validate(state, AT, p, financial)
    report = risk.report(state, AT)
    v = report.current.positions[0]
    assert v.open_quantity == Decimal("10")
    assert v.initial_assigned.value == Decimal("250") and v.initial_assigned.status == "MODELED"
    assert v.isolated_wallet_balance.value == Decimal("250")
    assert v.unrealized.value == Decimal(unrealized)
    assert v.isolated_margin_balance.value == Decimal(balance)
    assert v.maintenance.value == Decimal(maintenance)
    assert v.buffer.value == Decimal(buffer)
    assert v.condition == condition
    assert v.availability == "MODELED"
    if balance == "0":
        assert v.margin_ratio.value is None
        assert report.sampled_breaches == 1
    assert report.current.liquidation == "NOT_SIMULATED"
    assert report.current.survival == "NOT_DETERMINABLE_FROM_SAMPLES"
    assert v.pending_order_reserve == v.free_capital == "NOT_AVAILABLE"
    assert len(financial.ledger.entries) == 1  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "reserve,possible,expected",
    [
        (None, None, "UNKNOWN"),
        ("100", True, "INSUFFICIENT_MODEL_RESERVE"),
        ("108", True, "POSSIBLE_UNDER_EXPLICIT_ASSUMPTIONS"),
        ("1000", False, "UNKNOWN"),
    ],
)
def test_numeric_d_hypothesis_does_not_transfer(
    reserve: str | None, possible: bool | None, expected: str
) -> None:
    request = RiskValuationEvidence(
        stamp("hypothesis", sequence=1),
        RID,
        Decimal("100"),
        Decimal("100"),
        None if reserve is None else Decimal(reserve),
        possible,
    )
    risk, state, _, _ = numeric("75", events=(schedule(), request))
    v = risk.report(state, AT).current.positions[0]
    assert v.hypothesis is not None
    h = v.hypothesis
    assert h.buffer_before.value == Decimal("-7.50")
    assert h.buffer_after.value == Decimal("92.50")
    assert h.margin_balance_after.value == Decimal("100")
    assert h.target_met_by_hypothesis is False
    assert h.required_for_target.value == h.reserve_required.value == Decimal("107.50")
    assert h.feasibility == expected
    assert h.threshold_after.value is not None and h.threshold_before.value is not None
    assert h.threshold_after.value < h.threshold_before.value
    assert v.isolated_wallet_balance.value == Decimal("250")  # sigue sin transferencia
    assert v.additional_assigned.value == 0


@pytest.mark.parametrize("disabled", ["mark", "schedule", "allocation", "funding", "fee"])
def test_cases_e_f_and_unknown_costs(disabled: str) -> None:
    risk, state, _, _ = numeric(
        events=() if disabled == "schedule" else None,
        remove=disabled if disabled in {"mark", "funding", "fee"} else None,
        allocation=replace(margin_policy(), costs="UNKNOWN") if disabled == "allocation" else None,
    )
    v = risk.report(state, AT).current.positions[0]
    assert v.buffer.value is None and v.condition == "UNKNOWN"
    if disabled == "allocation":
        assert v.isolated_wallet_balance.value is None
        assert v.allocated_fees.value is None and v.allocated_funding.value is None
    if disabled == "schedule":
        assert v.maintenance.value is None
    assert risk.report(state, AT).information == "INSUFFICIENT_INFORMATION"


@pytest.mark.parametrize("kind", ["stale", "discontinuous", "unknown"])
def test_mark_not_replaced_with_trade_prices(kind: str) -> None:
    risk, state, p, financial = numeric()
    original = financial.events[-1].payload
    assert isinstance(original, MarkEvidence)
    if kind == "stale":
        at = AT + timedelta(milliseconds=60001)
        assert risk.report(state, at).current.positions[0].buffer.value is None
        return
    changed = (
        replace(original, continuous=False)
        if kind == "discontinuous"
        else replace(original, reference=replace(FIXTURE, provenance=Origin.UNKNOWN))
    )
    rebuilt = p.initial(START, END)
    for item in financial.journal:
        if getattr(item.payload, "payload", None) == original:
            item = replace(item, payload=replace(financial.events[-1], payload=changed))
        rebuilt = p.observe(rebuilt, item)
    ctx = risk.context_from(p, rebuilt, AT)
    v = risk.report(state, AT, ctx).current.positions[0]
    assert v.unrealized.value is None and v.buffer.value is None


@pytest.mark.parametrize("sold", ["2", "10"])
def test_partial_and_flat_explicit_release(sold: str) -> None:
    raw = apply(
        FinancialLedger(), execution(1, Side.BUY, "10", "100"), execution(2, Side.SELL, sold, "110")
    )
    risk, state, _, financial = numeric(ledger=raw)
    v = risk.report(state, AT).current.positions[0]
    assert v.open_quantity == Decimal("10") - Decimal(sold)
    assert v.initial_released.value == Decimal(sold) * Decimal("25")
    assert v.isolated_wallet_balance.value == Decimal("250") - v.initial_released.value
    assert v.fill_ids == tuple(e.fill_id for e in raw.entries)
    assert v.open_cost_basis_usdc == raw.round_ledger[0].open_cost_basis_usdc
    if sold == "10":
        assert v.condition == "NO_OPEN_POSITION"
        assert v.maintenance.value == 0 and v.threshold_price.value is None
    assert financial.ledger == raw


def test_no_principal_release_without_policy_and_no_hidden_realized_allocation() -> None:
    raw = apply(
        FinancialLedger(), execution(1, Side.BUY, "10", "100"), execution(2, Side.SELL, "2", "110")
    )
    risk, state, _, _ = numeric(
        ledger=raw, allocation=replace(margin_policy(), release="EXPLICIT_ONLY")
    )
    v = risk.report(state, AT).current.positions[0]
    assert v.initial_released.value == 0 and v.isolated_wallet_balance.value == Decimal("250")
    risk, state, _, _ = numeric(ledger=raw, allocation=replace(margin_policy(), realized="UNKNOWN"))
    assert risk.report(state, AT).current.positions[0].isolated_wallet_balance.value is None
    risk, state, _, _ = numeric(
        ledger=raw, allocation=replace(margin_policy(), realized="TO_ISOLATED")
    )
    assert risk.report(state, AT).current.positions[0].isolated_wallet_balance.value == Decimal(
        "220"
    )


def test_reconciled_costs_are_partitioned_once_not_subtracted_from_account_twice() -> None:
    f = FundingEvidence(
        "fund",
        SYMBOL,
        AT - timedelta(seconds=1),
        RID,
        Decimal("10"),
        FIXTURE,
        amount_usdc=Decimal("-3"),
    )
    risk, state, p, financial = numeric(amounts=("2",), funding=f)
    value = risk.report(state, AT).current
    v = value.positions[0]
    assert v.allocated_fees.value == Decimal("2") and v.allocated_funding.value == Decimal("-3")
    assert v.isolated_wallet_balance.value == Decimal("245")
    assert value.account_wallet.value == Decimal("995")
    assert value.account_wallet == p.value(financial, AT).wallet
    assert v.buffer.value == Decimal("185.50")


def test_explicit_balance_includes_costs_no_double_subtraction() -> None:
    raw = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    evidence = IsolatedMarginEvidence(
        stamp("isolated", sequence=1),
        RID,
        Decimal("248"),
        Decimal("250"),
        raw.processed_fill_ids,
        Decimal("2"),
        Decimal("0"),
    )
    allocation = replace(
        margin_policy(),
        initial="EVIDENCE_ONLY",
        release="EXPLICIT_ONLY",
        costs="INCLUDED_IN_BALANCE_EVIDENCE",
        reference=FIXTURE,
    )
    risk, state, p, financial = numeric(
        ledger=raw, amounts=("2",), events=(schedule(), evidence), allocation=allocation
    )
    v = risk.report(state, AT).current.positions[0]
    assert v.isolated_wallet_balance.value == Decimal("248")
    assert v.allocated_fees.value == Decimal("2")
    risk.validate(state, AT, p, financial)
    missing = replace(evidence, included_fees_usdc=None)
    risk, state, _, _ = numeric(
        ledger=raw, amounts=("2",), events=(schedule(), missing), allocation=allocation
    )
    assert risk.report(state, AT).current.positions[0].isolated_wallet_balance.value is None


@pytest.mark.parametrize("kind", ["ADD", "WITHDRAW"])
def test_adjustment_idempotence_and_conflict(kind: str) -> None:
    adjustment = MarginAdjustmentEvidence(stamp("adjust", sequence=1), RID, Decimal("100"), kind)
    risk, state, _, _ = numeric(events=(schedule(), adjustment))
    v = risk.report(state, AT).current.positions[0]
    assert v.isolated_wallet_balance.value == Decimal("350" if kind == "ADD" else "150")
    duplicate = replace(
        adjustment,
        stamp=replace(adjustment.stamp, received_at=AT + timedelta(seconds=1), sequence=9),
    )
    assert risk.observe(state, RiskObservation(duplicate.stamp.received_at, duplicate)) == state
    changed = replace(duplicate, amount_usdc=Decimal("101"))
    conflict = risk.observe(state, RiskObservation(changed.stamp.received_at, changed))
    assert conflict.conflicts and conflict.journal == state.journal
    assert (
        risk.report(conflict, changed.stamp.received_at).current.positions[0].buffer.status
        == "BLOCKED"
    )


def test_late_adjustment_blocks_without_rewriting_published_prefix() -> None:
    risk, state, _, _ = numeric()
    item = MarginAdjustmentEvidence(stamp("late", sequence=1), RID, Decimal("1"), "ADD")
    item = replace(
        item,
        stamp=replace(
            item.stamp,
            effective_at=AT - timedelta(seconds=1),
            received_at=AT + timedelta(seconds=1),
        ),
    )
    changed = risk.observe(state, RiskObservation(item.stamp.received_at, item))
    assert changed.conflicts and "tardía" in changed.conflicts[0].reason
    assert changed.evaluations == state.evaluations


def test_solver_changes_bracket_and_honors_exact_boundaries() -> None:
    synthetic = MaintenanceBracketSchedule(
        stamp("two"),
        START,
        END,
        (
            MaintenanceBracket(Decimal("0"), Decimal("1000"), Decimal(".01"), Decimal("0")),
            MaintenanceBracket(Decimal("1000"), Decimal("2000"), Decimal(".02"), Decimal("10")),
        ),
    )
    from trading_engine.replay.financial_performance.core import metric

    root = threshold(
        Decimal("10"),
        Decimal("1000"),
        metric(Decimal("10")),
        synthetic,
        enabled=True,
        leverage=Decimal("4"),
    )
    assert root.value == Decimal("100")
    risk, state, _, _ = numeric(
        "100", events=(synthetic,), allocation=replace(margin_policy(), release="EXPLICIT_ONLY")
    )
    assert risk.report(state, AT).current.positions[0].maintenance.value == Decimal("10")


@pytest.mark.parametrize(
    "field,value",
    [
        ("notional_floor", Decimal("-1")),
        ("notional_cap", Decimal("0")),
        ("maintenance_rate", Decimal("-0.01")),
        ("maintenance_rate", Decimal("1")),
        ("maintenance_amount", Decimal("-1")),
        ("maintenance_amount", Decimal("1")),
        ("max_leverage", Decimal("0")),
        ("maintenance_rate", 0.01),
        ("notional_cap", Decimal("Infinity")),
    ],
)
def test_invalid_bracket(field: str, value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(schedule().brackets[0], **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("floor", ["900", "1100"])
def test_overlaps_and_discontinuous_ranges_rejected(floor: str) -> None:
    with pytest.raises(ValueError):
        replace(
            schedule(),
            brackets=(
                MaintenanceBracket(Decimal("0"), Decimal("1000"), Decimal(".01"), Decimal("0")),
                MaintenanceBracket(Decimal(floor), Decimal("2000"), Decimal(".02"), Decimal("0")),
            ),
        )


@pytest.mark.parametrize("change", ["expired", "future_validity", "cap", "leverage"])
def test_schedule_coverage_not_invented(change: str) -> None:
    item = schedule()
    if change == "expired":
        item = replace(item, valid_until=AT)
    elif change == "future_validity":
        item = replace(item, valid_from=AT + timedelta(seconds=1))
    elif change == "cap":
        item = replace(item, brackets=(replace(item.brackets[0], notional_cap=Decimal("950")),))
    else:
        item = replace(item, brackets=(replace(item.brackets[0], max_leverage=Decimal("3")),))
    risk, state, _, _ = numeric(events=(item,))
    v = risk.report(state, AT).current.positions[0]
    assert v.maintenance.value is None and v.condition == "UNKNOWN"


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", "USDT"),
        ("symbol", Symbol("SHIBUSDC")),
        ("leverage", Decimal("8")),
        ("leverage", 4),
        ("rules_version", "future"),
        ("direction", "SHORT"),
        ("margin_mode", "CROSS"),
        ("position_mode", "HEDGE"),
        ("initial", "AUTO"),
        ("costs", "ASSUME_ZERO"),
        ("release", "ON_CANCEL"),
    ],
)
def test_wrong_contract_policy(field: str, value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(margin_policy(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", "USDT"),
        ("symbol", Symbol("BTCUSDT")),
        ("effective_at", AT + timedelta(seconds=1)),
        ("sequence", -1),
        ("sequence", True),
        ("version", ""),
        ("event_id", ""),
    ],
)
def test_invalid_evidence_stamp(field: str, value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(stamp("invalid"), **{field: value})  # type: ignore[arg-type]


def test_observed_not_authorized_by_label() -> None:
    with pytest.raises(ValueError, match="independiente"):
        Reference("Binance", "unverified", Origin.OBSERVED, "A label is not authentication")


def test_decimal50_and_hostile_context_exactly_identical() -> None:
    expected = numeric()[1]
    with localcontext() as context:
        context.prec = 3
        context.Emax = 3
        context.Emin = -3
        context.rounding = "ROUND_UP"
        context.traps[Inexact] = True
        actual = numeric()[1]
    assert actual == expected
    root = actual.evaluations[0].positions[0].threshold_price.value
    assert root is not None and len(root.as_tuple().digits) == 50


@pytest.mark.parametrize("damage", ["evaluations", "journal", "context", "events", "principal"])
def test_authoritative_reconstruction_rejects_forged_state(damage: str) -> None:
    risk, state, p, financial = numeric()
    if damage == "evaluations":
        altered = replace(state, evaluations=())
    elif damage == "journal":
        altered = replace(state, journal=())
    elif damage == "context":
        altered = replace(state, context=None)
    elif damage == "events":
        altered = replace(state, events=())
    else:
        point = state.evaluations[0]
        position = point.positions[0]
        altered = replace(
            state,
            evaluations=(
                replace(
                    point,
                    positions=(
                        replace(
                            position,
                            initial_assigned=replace(
                                position.initial_assigned, value=Decimal("1200")
                            ),
                        ),
                    ),
                ),
            ),
        )
    with pytest.raises(ValueError):
        risk.validate(altered, AT, p, financial)


def test_fill_identity_cannot_be_invented_for_balance_or_release() -> None:
    risk, state, _, _ = numeric()
    wrong = MarginAdjustmentEvidence(
        stamp("release", sequence=1), RID, Decimal("1"), "SELL_RELEASE", FillId(UUID(int=999))
    )
    assert risk.observe(state, RiskObservation(AT, wrong)).conflicts


def test_zero_buffer_is_threshold_not_ample_margin() -> None:
    risk, state, _, _ = numeric(
        "100",
        events=(
            schedule(),
            MarginAdjustmentEvidence(
                stamp("withdraw", sequence=1), RID, Decimal("240"), "WITHDRAW"
            ),
        ),
    )
    v = risk.report(state, AT).current.positions[0]
    assert v.buffer.value == 0 and v.condition == "AT_MAINTENANCE_THRESHOLD"
    assert v.margin_ratio.value == Decimal("1")


@pytest.mark.parametrize("kind", ["schedule", "add", "withdraw", "request"])
def test_new_events_cannot_rewrite_sealed_instant(kind: str) -> None:
    risk, state, _, _ = numeric()
    item: RiskEvent
    if kind == "schedule":
        item = replace(schedule(), stamp=stamp("another-schedule", sequence=10))
    elif kind == "request":
        item = RiskValuationEvidence(stamp("late-request", sequence=10), RID, Decimal("100"))
    else:
        item = MarginAdjustmentEvidence(
            stamp("late-change", sequence=10),
            RID,
            Decimal("1"),
            "ADD" if kind == "add" else "WITHDRAW",
        )
    changed = risk.observe(state, RiskObservation(AT, item))
    assert changed.conflicts and changed.evaluations == state.evaluations


def test_overlapping_schedule_economic_validities_block() -> None:
    first = schedule()
    second = replace(first, stamp=stamp("second", sequence=1))
    raw = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    financial = complete(p, raw)
    risk = IsolatedMarginRisk(margin_policy(), FixtureRiskSource((first, second)))
    state = risk.observe(
        risk.initial(START, END), RiskObservation(AT, risk.context_from(p, financial, AT))
    )
    state = risk.observe(state, RiskObservation(AT, first))
    state = risk.observe(state, RiskObservation(AT, second))
    assert state.conflicts and "solapadas" in state.conflicts[0].reason
    risk.validate(state, AT, p, financial)


@pytest.mark.parametrize("unknown", ["adjustment", "hypothesis", "schedule"])
def test_unknown_provenance_never_authorizes_numeric_effect(unknown: str) -> None:
    item: RiskEvent
    if unknown == "adjustment":
        item = MarginAdjustmentEvidence(stamp("unknown", sequence=1), RID, Decimal("100"), "ADD")
    elif unknown == "hypothesis":
        item = RiskValuationEvidence(stamp("unknown", sequence=1), RID, Decimal("100"))
    else:
        item = replace(schedule(), stamp=stamp("unknown", sequence=1))
    item = replace(
        item, stamp=replace(item.stamp, reference=replace(FIXTURE, provenance=Origin.UNKNOWN))
    )
    # La fuente fixture no puede afirmar UNKNOWN; un port distinto puede
    # entregarlo y el motor lo conserva sin autorizar cantidades.
    risk, state, _, _ = numeric(events=())
    unsealed = replace(state, evaluations=(), journal=state.journal[:-1])
    changed = risk.observe(unsealed, RiskObservation(AT, item))
    assert not changed.conflicts
    changed = risk.observe(changed, RiskObservation(AT, "VALUE"))
    v = risk.report(changed, AT).current.positions[0]
    if unknown == "adjustment":
        assert v.isolated_wallet_balance.value is None
    elif unknown == "hypothesis":
        assert v.hypothesis is not None and v.hypothesis.buffer_after.value is None
    else:
        assert v.maintenance.value is None
    with pytest.raises(ValueError):
        FixtureRiskSource((item,))


@pytest.mark.parametrize("disable", ["solver", "linear"])
def test_sensitivity_requires_explicit_policy(disable: str) -> None:
    allocation = (
        replace(margin_policy(), threshold_solver=False)
        if disable == "solver"
        else replace(margin_policy(), linear_additional_margin=False)
    )
    request = RiskValuationEvidence(stamp("hyp", sequence=1), RID, Decimal("100"), Decimal("100"))
    risk, state, _, _ = numeric(events=(schedule(), request), allocation=allocation)
    v = risk.report(state, AT).current.positions[0]
    assert v.hypothesis is not None
    if disable == "solver":
        assert v.threshold_price.value is None and v.hypothesis.threshold_after.value is None
    else:
        assert (
            v.hypothesis.buffer_after.value is None
            and v.hypothesis.required_for_target.value is None
        )


@pytest.mark.parametrize("quantity,wallet", [("10", "1000"), ("0", "250"), ("10", "-10000")])
def test_solver_no_verifiable_positive_root_is_unavailable(quantity: str, wallet: str) -> None:
    from trading_engine.replay.financial_performance.core import metric

    value = threshold(
        Decimal(quantity),
        Decimal("1000"),
        metric(Decimal(wallet)),
        schedule(),
        enabled=True,
        leverage=Decimal("4"),
    )
    if wallet != "-10000":
        assert value.value is None
    else:
        limited = replace(
            schedule(), brackets=(replace(schedule().brackets[0], notional_cap=Decimal("2000")),)
        )
        assert (
            threshold(
                Decimal(quantity),
                Decimal("1000"),
                metric(Decimal(wallet)),
                limited,
                enabled=True,
                leverage=Decimal("4"),
            ).value
            is None
        )


def test_multiple_piecewise_roots_are_not_claimed_as_unique_threshold() -> None:
    from trading_engine.replay.financial_performance.core import metric

    item = replace(
        schedule(),
        brackets=(
            MaintenanceBracket(Decimal("0"), Decimal("1000"), Decimal(".1"), Decimal("0")),
            MaintenanceBracket(Decimal("1000"), Decimal("3000"), Decimal(".5"), Decimal("0")),
        ),
    )
    # raíz 888.8 de nocional en primer tramo, 1600 en segundo:
    # un salto de mantenimiento impide un umbral único verificable.
    value = threshold(
        Decimal("10"),
        Decimal("1000"),
        metric(Decimal("200")),
        item,
        enabled=True,
        leverage=Decimal("4"),
    )
    assert value.value is None


def test_schedule_boundary_formula_and_source_sequences_are_explicit() -> None:
    for field, value in (("boundaries", "CAP_INCLUSIVE"), ("formula", "BINANCE_UNVERIFIED")):
        with pytest.raises(ValueError):
            replace(schedule(), **{field: value})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FixtureRiskSource((schedule(), schedule()))
    with pytest.raises(ValueError):
        replace(margin_policy(), reference=FIXTURE)


def test_negative_margin_balance_never_has_deceptive_positive_ratio() -> None:
    risk, state, _, _ = numeric("74")
    v = risk.report(state, AT).current.positions[0]
    assert v.isolated_margin_balance.value == Decimal("-10")
    assert v.condition == "BELOW_MAINTENANCE" and v.margin_ratio.value is None


def test_explicit_sell_release_needs_fill_and_does_not_double_proportional_release() -> None:
    raw = apply(
        FinancialLedger(), execution(1, Side.BUY, "10", "100"), execution(2, Side.SELL, "2", "110")
    )
    release = MarginAdjustmentEvidence(
        stamp("released", sequence=1), RID, Decimal("50"), "SELL_RELEASE", raw.entries[1].fill_id
    )
    risk, state, _, _ = numeric(
        ledger=raw,
        events=(schedule(), release),
        allocation=replace(margin_policy(), release="EXPLICIT_ONLY"),
    )
    v = risk.report(state, AT).current.positions[0]
    assert v.open_quantity == Decimal("8")
    assert v.initial_released.value == 0 and v.sell_adjustments.value == Decimal("50")
    assert v.isolated_wallet_balance.value == Decimal("200")
    # El mismo hecho con otra identidad no puede volver a liberar la venta.
    next_at = AT + timedelta(seconds=1)
    duplicate = replace(
        release,
        stamp=replace(
            release.stamp,
            event_id="other-release",
            effective_at=next_at,
            received_at=next_at,
            sequence=2,
        ),
    )
    assert risk.observe(state, RiskObservation(next_at, duplicate)).conflicts


def test_risk_identity_deterministic_and_unknown_policy_rejected() -> None:
    source = FixtureRiskSource((schedule(),))
    assert (
        IsolatedMarginRisk(margin_policy(), source).identity
        == IsolatedMarginRisk(margin_policy(), source).identity
    )
    changed = replace(
        margin_policy(), reference=replace(MODELED, assumptions="A different explicit hypothesis")
    )
    assert (
        IsolatedMarginRisk(changed, source).identity
        != IsolatedMarginRisk(margin_policy(), source).identity
    )
    with pytest.raises(ValueError):
        replace(margin_policy(), reference=replace(MODELED, provenance=Origin.UNKNOWN))


def test_nonrepresentable_margin_blocks_and_remains_auditable() -> None:
    raw = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    p = FinancialPerformance(policy())
    financial = complete(p, raw)
    first_stamp = replace(stamp("large-1", sequence=1), effective_at=AT - timedelta(seconds=1))
    large = Decimal("9e999999999999999999")
    events = (
        schedule(),
        MarginAdjustmentEvidence(first_stamp, RID, large, "ADD"),
        MarginAdjustmentEvidence(stamp("large-2", sequence=2), RID, large, "ADD"),
    )
    risk = IsolatedMarginRisk(margin_policy(), FixtureRiskSource(events))
    state = risk.observe(
        risk.initial(START, END), RiskObservation(AT, risk.context_from(p, financial, AT))
    )
    for value in events:
        state = risk.observe(state, RiskObservation(AT, value))
    state = risk.observe(state, RiskObservation(AT, "VALUE"))
    assert state.conflicts and "Overflow" in state.conflicts[0].reason
    assert state.context is not None and state.context.ledger == raw
    assert state.context.ledger.round_ledger[0].open_quantity == Decimal("10")
    assert risk.report(state, AT).information == "BLOCKED"
    risk.validate(state, AT, p, financial)


def test_absolute_balance_does_not_claim_unchanged_collateral_at_future_time() -> None:
    raw = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    evidence = IsolatedMarginEvidence(
        stamp("point-balance", sequence=1),
        RID,
        Decimal("250"),
        Decimal("250"),
        raw.processed_fill_ids,
        Decimal("0"),
        Decimal("0"),
    )
    allocation = replace(
        margin_policy(),
        initial="EVIDENCE_ONLY",
        release="EXPLICIT_ONLY",
        costs="INCLUDED_IN_BALANCE_EVIDENCE",
        reference=FIXTURE,
    )
    risk, state, p, _ = numeric(ledger=raw, events=(schedule(), evidence), allocation=allocation)
    assert risk.report(state, AT).current.positions[0].isolated_wallet_balance.value == Decimal(
        "250"
    )
    later = AT + timedelta(seconds=1)
    # Mark, fees y funding siguen completos; falta exclusivamente una nueva
    # prueba del saldo aislado, no se extrapola un snapshot monetario puntual.
    financial = complete(p, raw, at=later)
    point = risk.report(state, later, risk.context_from(p, financial, later)).current.positions[0]
    assert point.unrealized.value == Decimal("-50") and point.maintenance.value == Decimal("9.50")
    assert point.isolated_wallet_balance.value is None and point.condition == "UNKNOWN"
