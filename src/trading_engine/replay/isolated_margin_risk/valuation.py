"""Proyección del ledger confirmado y partición explícita de balances."""

from datetime import datetime, timedelta
from decimal import Decimal

from trading_engine.models.enums import Side
from trading_engine.replay.financial_ledger.models import LedgerEntry, RoundLedger
from trading_engine.replay.financial_performance.core import dependent, metric
from trading_engine.replay.financial_performance.evidence import Origin
from trading_engine.replay.financial_performance.models import Metric, RoundPerformance
from trading_engine.replay.isolated_margin_risk.evidence import (
    IsolatedMarginEvidence,
    MaintenanceBracketSchedule,
    MarginAdjustmentEvidence,
    RiskValuationEvidence,
)
from trading_engine.replay.isolated_margin_risk.models import (
    IsolatedPositionRisk,
    MarginHypothesis,
    MarginTransition,
    RiskContext,
    RiskEvaluation,
    RiskState,
)
from trading_engine.replay.isolated_margin_risk.solver import threshold

ZERO = Decimal("0")


def unavailable(reason: str) -> Metric:
    return metric(None, (), reason)


def principal(
    state: RiskState, entries: tuple[LedgerEntry, ...]
) -> tuple[Metric, Metric, tuple[MarginTransition, ...]]:
    if state.policy.initial != "EXECUTED_NOTIONAL_OVER_LEVERAGE":
        return unavailable("INITIAL_MARGIN_EVIDENCE_REQUIRED"), unavailable("RELEASE_UNKNOWN"), ()
    assigned = released = current = ZERO
    transitions = []
    for entry in entries:
        addition = reduction = ZERO
        if entry.side is Side.BUY:
            addition = entry.notional_usdc / state.policy.leverage
            if addition <= 0:
                raise ValueError("Margen inicial positivo no representable")
            assigned += addition
            current += addition
        elif state.policy.release == "PROPORTIONAL_INITIAL_ON_SELL":
            before = entry.open_quantity_after + entry.quantity
            reduction = (
                current if entry.open_quantity_after == 0 else current * (entry.quantity / before)
            )
            current -= reduction
            # Acumulado canónico ligado al principal restante; cierre exacto
            # sin residuos por sumar parciales redondeados en distinto orden.
            released = assigned - current
        transitions.append(
            MarginTransition(
                entry.fill_id, entry.round_id, entry.received_at, addition, reduction, current
            )
        )
    origins = (Origin.MODELED,)
    return metric(assigned, origins), metric(released, origins), tuple(transitions)


def position(
    state: RiskState,
    context: RiskContext,
    round_ledger: RoundLedger,
    financial: RoundPerformance,
    at: datetime,
) -> IsolatedPositionRisk:
    policy = state.policy
    rid = round_ledger.round_id
    entries = tuple(e for e in context.ledger.entries if e.round_id == rid)
    ids = tuple(e.fill_id for e in entries)
    q, cost = round_ledger.open_quantity, round_ledger.open_cost_basis_usdc
    assigned, released, transitions = principal(state, entries)
    adjustments = tuple(
        e for e in state.events if isinstance(e, MarginAdjustmentEvidence) and e.round_id == rid
    )

    def adjustment(kind: str) -> Metric:
        relevant = tuple(e for e in adjustments if e.kind == kind)
        origins = tuple(e.stamp.reference.provenance for e in relevant)
        if Origin.UNKNOWN in origins:
            return metric(None, origins, "MARGIN_ADJUSTMENT_UNKNOWN")
        return metric(sum((e.amount_usdc for e in relevant), ZERO), origins)

    added, withdrawn, sell = (adjustment(kind) for kind in ("ADD", "WITHDRAW", "SELL_RELEASE"))
    fees, funding = financial.fees, financial.funding
    wallet = unavailable("ISOLATED_ALLOCATION_UNKNOWN")
    if policy.initial == "EXECUTED_NOTIONAL_OVER_LEVERAGE":
        realized = (
            unavailable("REALIZED_ALLOCATION_UNKNOWN")
            if policy.realized == "UNKNOWN" and any(e.side is Side.SELL for e in entries)
            else financial.gross_realized
            if policy.realized == "TO_ISOLATED"
            else metric(ZERO, (Origin.MODELED,))
        )
        if policy.costs == "RECONCILED_TO_ISOLATED":
            wallet = dependent(
                (assigned.value or ZERO)
                - (released.value or ZERO)
                + (added.value or ZERO)
                - (withdrawn.value or ZERO)
                - (sell.value or ZERO)
                + (realized.value or ZERO)
                - (fees.value or ZERO)
                + (funding.value or ZERO),
                assigned,
                released,
                added,
                withdrawn,
                sell,
                realized,
                fees,
                funding,
            )
    else:
        balances = tuple(
            e for e in state.events if isinstance(e, IsolatedMarginEvidence) and e.round_id == rid
        )
        evidence = balances[-1] if balances else None
        if evidence is not None:
            assigned = metric(
                evidence.initial_assigned_usdc, (evidence.stamp.reference.provenance,)
            )
            # Balance absoluto está vinculado al conjunto exacto de fills.
            # Ya incluye sus costos: no se vuelven a restar aquí.
            if (
                policy.costs == "INCLUDED_IN_BALANCE_EVIDENCE"
                and evidence.stamp.reference.provenance is not Origin.UNKNOWN
                and evidence.stamp.effective_at == at
                and evidence.stamp.received_at == at
                and evidence.fill_ids == ids
                and fees.value is not None
                and funding.value is not None
                and evidence.included_fees_usdc == fees.value
                and evidence.included_funding_usdc == funding.value
            ):
                wallet = metric(
                    evidence.wallet_balance_usdc,
                    (evidence.stamp.reference.provenance,) + fees.provenances + funding.provenances,
                )
            else:
                wallet = unavailable("BALANCE_ASOF_FILL_OR_COST_RECONCILIATION_MISSING")
    if policy.costs == "UNKNOWN":
        fees = unavailable("FEE_ALLOCATION_UNKNOWN")
        funding = unavailable("FUNDING_ALLOCATION_UNKNOWN")

    mark = context.mark
    valid_mark = (
        mark is not None
        and mark.reference.provenance is not Origin.UNKNOWN
        and mark.continuous
        and mark.effective_at
        <= at
        <= mark.effective_at + timedelta(milliseconds=mark.freshness_limit_ms)
    )
    notional = unrealized = unavailable("MARK_MISSING_STALE_OR_DISCONTINUOUS")
    if q == 0:
        notional = unrealized = metric(ZERO)
    elif valid_mark:
        assert mark is not None
        value = q * mark.price
        if value <= 0:
            raise ValueError("Nocional Mark positivo no representable")
        notional = metric(value, (mark.reference.provenance,))
        unrealized = metric(value - cost, (mark.reference.provenance, Origin.MODELED))
    schedules = tuple(
        e
        for e in state.events
        if isinstance(e, MaintenanceBracketSchedule)
        and e.valid_from <= at < e.valid_until
        and e.stamp.reference.provenance is not Origin.UNKNOWN
    )
    schedule = schedules[0] if len(schedules) == 1 else None
    maintenance = unavailable("MAINTENANCE_SCHEDULE_OR_NOTIONAL_COVERAGE_MISSING")
    if q == 0:
        maintenance = metric(ZERO)
    elif schedule is not None and notional.value is not None:
        bracket = next((b for b in schedule.brackets if b.contains(notional.value)), None)
        if bracket is not None and (
            bracket.max_leverage is None or policy.leverage <= bracket.max_leverage
        ):
            product = notional.value * bracket.maintenance_rate
            if bracket.maintenance_rate > 0 and product <= 0:
                raise ValueError("Mantenimiento positivo no representable")
            value = product - bracket.maintenance_amount
            if value < 0:
                raise ValueError("Mantenimiento negativo")
            maintenance = metric(
                value, notional.provenances + (schedule.stamp.reference.provenance, Origin.MODELED)
            )
    balance = dependent((wallet.value or ZERO) + (unrealized.value or ZERO), wallet, unrealized)
    buffer = dependent((balance.value or ZERO) - (maintenance.value or ZERO), balance, maintenance)
    ratio = unavailable("MARGIN_BALANCE_NONPOSITIVE_OR_UNKNOWN")
    if balance.value is not None and balance.value > 0 and maintenance.value is not None:
        ratio = dependent(maintenance.value / balance.value, balance, maintenance)
    price = threshold(
        q, cost, wallet, schedule, enabled=policy.threshold_solver, leverage=policy.leverage
    )
    distance = unavailable("MARK_OR_THRESHOLD_MISSING")
    if valid_mark and price.value is not None:
        assert mark is not None
        distance = dependent(
            mark.price - price.value, price, metric(mark.price, (mark.reference.provenance,))
        )
    condition = (
        "NO_OPEN_POSITION"
        if q == 0
        else "UNKNOWN"
        if buffer.value is None
        else "ABOVE_MAINTENANCE"
        if buffer.value > 0
        else "AT_MAINTENANCE_THRESHOLD"
        if buffer.value == 0
        else "BELOW_MAINTENANCE"
    )
    availability = (
        "AVAILABLE"
        if q == 0
        else buffer.status
        if buffer.value is not None
        else "INCOMPLETE"
        if any(m.value is not None for m in (wallet, maintenance, unrealized))
        else "NOT_AVAILABLE"
    )
    hypothesis = None
    request = next(
        (
            e
            for e in reversed(state.events)
            if isinstance(e, RiskValuationEvidence)
            and e.round_id == rid
            and e.stamp.received_at == at
        ),
        None,
    )
    if request is not None:
        hypothesis = sensitivity(state, request, q, cost, wallet, balance, buffer, price, schedule)
    return IsolatedPositionRisk(
        rid,
        ids,
        q,
        cost,
        assigned,
        released,
        added,
        withdrawn,
        sell,
        fees,
        funding,
        wallet,
        notional,
        unrealized,
        balance,
        maintenance,
        buffer,
        ratio,
        price,
        distance,
        condition,
        availability,
        None if mark is None else mark.event_id,
        None if schedule is None else schedule.stamp.event_id,
        transitions,
        hypothesis,
    )


def sensitivity(
    state: RiskState,
    request: RiskValuationEvidence,
    q: Decimal,
    cost: Decimal,
    wallet: Metric,
    balance: Metric,
    buffer: Metric,
    price: Metric,
    schedule: MaintenanceBracketSchedule | None,
) -> MarginHypothesis:
    missing = unavailable("LINEAR_SENSITIVITY_INPUT_OR_POLICY_MISSING")
    enabled = (
        state.policy.linear_additional_margin
        and request.stamp.reference.provenance is not Origin.UNKNOWN
    )
    extra = (
        metric(request.additional_margin_usdc, (Origin.MODELED,))
        if enabled and request.additional_margin_usdc is not None
        else missing
    )
    after = dependent((buffer.value or ZERO) + (extra.value or ZERO), buffer, extra)
    after_balance = dependent((balance.value or ZERO) + (extra.value or ZERO), balance, extra)
    after_wallet = dependent((wallet.value or ZERO) + (extra.value or ZERO), wallet, extra)
    after_price = threshold(
        q,
        cost,
        after_wallet,
        schedule,
        enabled=state.policy.threshold_solver,
        leverage=state.policy.leverage,
    )
    required = missing
    if enabled and buffer.value is not None and request.target_buffer_usdc is not None:
        required = metric(max(ZERO, request.target_buffer_usdc - buffer.value), (Origin.MODELED,))
    # Para un objetivo se informa su mínimo; para un importe, el importe supuesto.
    reserve = required if request.target_buffer_usdc is not None else extra
    feasible = "UNKNOWN"
    if (
        enabled
        and request.transfer_assumed_possible is True
        and request.reserve_available_usdc is not None
        and reserve.value is not None
    ):
        feasible = (
            "POSSIBLE_UNDER_EXPLICIT_ASSUMPTIONS"
            if request.reserve_available_usdc >= reserve.value
            else "INSUFFICIENT_MODEL_RESERVE"
        )
    return MarginHypothesis(
        request.stamp.event_id,
        extra,
        buffer,
        after,
        after_balance,
        price,
        after_price,
        required,
        reserve,
        feasible,
        None
        if after.value is None or request.target_buffer_usdc is None
        else after.value >= request.target_buffer_usdc,
    )


def evaluate(state: RiskState, context: RiskContext, at: datetime) -> RiskEvaluation:
    rounds = {r.round_id: r for r in context.financial.rounds}
    positions = tuple(
        position(state, context, r, rounds[r.round_id], at) for r in context.ledger.round_ledger
    )
    return RiskEvaluation(at, positions, context.financial.wallet, context.financial.equity)
