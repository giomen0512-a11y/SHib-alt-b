"""Conservación, temporalidad y fallos; exclusivamente fixtures sintéticos."""

import ast
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from uuid import UUID

import pytest

from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.ids import IntentId, OrderId
from trading_engine.models.orders import Order, OrderCancelRequest, OrderIntent
from trading_engine.models.values import Price, Quantity, Symbol
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import Origin
from trading_engine.replay.financial_performance.models import PerformanceState
from trading_engine.replay.isolated_margin_risk import IsolatedMarginRisk
from trading_engine.replay.isolated_margin_risk.models import RiskObservation, RiskState
from trading_engine.replay.reserve_transfer_simulation import (
    AutoMarginPolicy,
    ReservationPolicy,
    ReserveTransferSimulation,
)
from trading_engine.replay.reserve_transfer_simulation.evidence import (
    CapitalPartitionEvidence,
    FixtureReserveSource,
    OrderPriceEvidence,
    TransferCommandEvidence,
)
from trading_engine.replay.reserve_transfer_simulation.models import ReserveState
from trading_engine.replay.reserve_transfer_simulation.orders import reservations

from .conftest import SYMBOL
from .test_financial_ledger import apply, execution
from .test_financial_performance import AT, FIXTURE, MODELED, RID, complete, policy
from .test_isolated_margin_risk import numeric, stamp

D = Decimal


def auto(*, latency: int = 120000, enabled: bool = True, timeout: int = 180000) -> AutoMarginPolicy:
    return AutoMarginPolicy(
        trigger_buffer_usdc=D(50),
        target_buffer_usdc=D(142),
        minimum_transfer_usdc=D(1),
        maximum_transfer_usdc=D(100),
        max_per_round_usdc=D(100),
        max_simultaneous_requests=1,
        request_latency_ms=0,
        confirmation_latency_ms=latency,
        timeout_ms=timeout,
        retry_policy="NO_RETRY",
        rounding_policy="EXACT_DECIMAL",
        source_reference=MODELED,
        enabled=enabled,
    )


def pending_order(
    *, qty: str = "4", market: bool = False, reduce: bool = False, sell: bool = False
) -> Order:
    intent = OrderIntent(
        IntentId(UUID(int=700)),
        RID,
        SYMBOL,
        Side.SELL if sell else Side.BUY,
        OrderType.MARKET if market else OrderType.LIMIT,
        Quantity(D(qty)),
        AT,
        None if market else Price(D(100)),
        reduce,
    )
    return Order(OrderId(UUID(int=700)), intent, OrderStatus.OPEN, D(0), AT, AT)


def scenario(
    *,
    latency: int = 120000,
    free: str = "650",
    enabled: bool = True,
    remove: str | None = None,
    price: str = "80",
    market: bool = False,
    timeout: int = 180000,
) -> tuple[
    ReserveTransferSimulation,
    ReserveState,
    IsolatedMarginRisk,
    RiskState,
    FinancialPerformance,
    PerformanceState,
]:
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100", requested="10"))
    risk, rs, perf, financial = numeric(price, remove=remove, ledger=ledger)
    assert rs.context is not None
    order = pending_order(market=market)
    with localcontext() as context:
        context.prec = 50
        wallet = D(free) + D(350)
    if wallet != 1000:
        perf = FinancialPerformance(policy(capital=str(wallet)))
        financial = complete(perf, rs.context.ledger, price=price)
        rs = replace(rs, context=risk.context_from(perf, financial, AT))
    assert rs.context is not None
    capital = CapitalPartitionEvidence(
        stamp("initial-partition"),
        wallet,
        D(free),
        ((RID, D(250)),),
        ((order.order_id, D(100)),),
        (),
        tuple(e.fill_id for e in rs.context.ledger.entries),
    )
    controller = ReserveTransferSimulation(
        ReservationPolicy(MODELED, "EXPLICIT_PRICE_EVIDENCE"),
        auto(latency=latency, enabled=enabled, timeout=timeout),
        capital,
        initial_orders=(order,),
    )
    # El fixture de 4A-4A ya publicó VALUE; preparar el instante antes de esa publicación.
    rs = replace(rs, evaluations=(), journal=rs.journal[:-1])
    state, rs = controller.tick(controller.initial(), risk, rs, AT)
    return controller, state, risk, rs, perf, financial


def advance(
    controller: ReserveTransferSimulation,
    state: ReserveState,
    risk: IsolatedMarginRisk,
    rs: RiskState,
    perf: FinancialPerformance,
    at: datetime,
    price: str,
    *,
    ledger: object = None,
) -> tuple[ReserveState, RiskState]:
    assert rs.context is not None
    from trading_engine.replay.financial_ledger.models import LedgerState

    raw = rs.context.ledger if ledger is None else ledger
    assert isinstance(raw, LedgerState)
    financial = complete(perf, raw, price=price, at=at)
    rs = risk.observe(rs, RiskObservation(at, risk.context_from(perf, financial, at)))
    assert not rs.conflicts
    return controller.tick(state, risk, rs, at)


def assert_partition(
    state: ReserveState, free: str, isolated: str, order: str, transfer: str, wallet: str = "1000"
) -> None:
    p = state.partition
    assert p.status == "RECONCILED"
    assert p.free_balance.value == D(free)
    assert p.isolated_wallet_allocated.value == D(isolated)
    assert p.pending_order_holds.value == D(order)
    assert p.pending_transfer_holds.value == D(transfer)
    assert p.account_wallet.value == D(wallet)
    with localcontext() as ctx:
        ctx.prec = 50
        assert (
            D(wallet) == D(free) + D(isolated) + D(order) + D(transfer) + p.other_accounted_buckets
        )


def test_case_a_explicit_initial_partition() -> None:
    _, state, _, _, _, _ = scenario(enabled=False)
    assert_partition(state, "650", "250", "100", "0")
    assert not state.transfers and not state.adjustments


def test_cases_b_c_confirmation_preserves_wallet_and_pnl() -> None:
    c, state, risk, rs, p, f = scenario(latency=30000)
    assert_partition(state, "550", "250", "100", "100")
    assert state.transfers[0].state == "CONFIRMATION_PENDING"
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=30), "80")
    assert_partition(state, "550", "350", "100", "0")
    request = state.transfers[0]
    assert request.confirmed_usdc == D(100) and request.attempts == 1
    assert (
        request.confirmation is not None and request.confirmation.request_id == request.request_id
    )
    assert request.confirmation.hold_id == request.hold_id
    assert rs.context is not None
    assert rs.context.financial.wallet == f.series[-1].wallet
    assert rs.context.financial.gross_realized.value == 0
    assert rs.context.financial.net_cash.value == 0
    assert [
        e.kind
        for e in state.journal
        if e.kind
        in {
            "PROPOSED",
            "FUNDS_HELD",
            "REQUEST_PENDING",
            "CONFIRMATION_PENDING",
            "CONFIRMED_SIMULATED",
        }
    ] == [
        "PROPOSED",
        "FUNDS_HELD",
        "REQUEST_PENDING",
        "CONFIRMATION_PENDING",
        "CONFIRMED_SIMULATED",
    ]


def test_case_d_rejection_releases_once() -> None:
    c, state, risk, rs, _, _ = scenario()
    rid = state.transfers[0].request_id
    state, rs = c.terminal(state, risk, rs, AT, rid, "REJECT", reason="SYNTHETIC_REJECTION")
    assert_partition(state, "650", "250", "100", "0")
    assert not state.adjustments and not state.queue
    with pytest.raises(ValueError, match="terminal"):
        c.terminal(state, risk, rs, AT, rid, "REJECT", reason="duplicate")


def test_case_e_breach_before_late_confirmation_is_permanent() -> None:
    c, state, risk, rs, p, _ = scenario()
    assert state.risk is not None
    assert state.risk.positions[0].buffer.value == D(42)
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=60), "75")
    assert state.risk is not None
    v = state.risk.positions[0]
    assert (
        v.unrealized.value,
        v.isolated_margin_balance.value,
        v.maintenance.value,
        v.buffer.value,
    ) == (D(-250), D(0), D("7.5"), D("-7.5"))
    assert_partition(state, "550", "250", "100", "100")
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=120), "75")
    request = state.transfers[0]
    assert request.first_breach_at == AT + timedelta(seconds=60)
    assert request.first_breach_buffer == D("-7.5") and request.buffer_after == D("92.5")
    assert request.confirmed_at == AT + timedelta(seconds=120)
    assert risk.report(rs, request.confirmed_at).sampled_breaches == 1
    assert c.report(state).trajectory == "CONDITIONED_COUNTERFACTUAL_AFTER_SAMPLED_BREACH"
    assert c.report(state).interval_path == "UNKNOWN_INTRAMINUTE_PATH"


def test_case_f_timely_confirmation_only_protects_samples() -> None:
    c, state, risk, rs, p, _ = scenario(latency=30000)
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=30), "80")
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=60), "75")
    assert state.risk is not None
    v = state.risk.positions[0]
    assert (
        v.isolated_wallet_balance.value,
        v.isolated_margin_balance.value,
        v.maintenance.value,
        v.buffer.value,
    ) == (D(350), D(100), D("7.5"), D("92.5"))
    assert state.transfers[0].first_breach_at is None
    assert risk.report(rs, AT + timedelta(seconds=60)).sampled_breaches == 0
    assert c.report(state).liquidation == "NO_LIQUIDATION_SIMULATED"


def test_case_g_insufficient_free_no_invented_hold() -> None:
    _, state, _, _, _, _ = scenario(free="60")
    assert_partition(state, "60", "250", "100", "0", "410")
    assert state.transfers[0].state == "REJECTED"
    assert state.transfers[0].rejection_reason == "INSUFFICIENT_FREE_BALANCE"
    assert not state.queue and not state.adjustments


@pytest.mark.parametrize(
    "command,amount,offset",
    [("CONFIRM", "100", 0), ("CONFIRM", "101", 120), ("CONFIRM", "99", 120), ("EXPIRE", None, 1)],
)
def test_out_of_order_early_over_or_under_hold_and_early_timeout(
    command: str, amount: str | None, offset: int
) -> None:
    c, state, risk, rs, _, _ = scenario()
    with pytest.raises(ValueError):
        c.terminal(
            state,
            risk,
            rs,
            AT + timedelta(seconds=offset),
            state.transfers[0].request_id,
            command,
            None if amount is None else D(amount),
        )
    assert_partition(state, "550", "250", "100", "100")


def test_duplicate_proposal_idempotent_and_conflicting_duplicate_rejected() -> None:
    c, state, risk, _, _, _ = scenario()
    assert state.risk is not None
    req = state.transfers[0]
    assert c.propose(state, AT, state.risk.positions[0], D(100), req.request_id, risk) == state
    with pytest.raises(ValueError, match="duplicada"):
        c.propose(state, AT, state.risk.positions[0], D(99), req.request_id, risk)
    other = c.propose(state, AT, state.risk.positions[0], D(100), "incompatible", risk)
    assert other.transfers[-1].rejection_reason == "TRANSFER_NOT_ALLOWED:PENDING_REQUEST"
    assert other.partition == state.partition


def test_confirmation_duplicate_rejected() -> None:
    c, state, risk, rs, p, _ = scenario(latency=30000)
    at = AT + timedelta(seconds=30)
    state, rs = advance(c, state, risk, rs, p, at, "80")
    with pytest.raises(ValueError, match="terminal"):
        c.terminal(state, risk, rs, at, state.transfers[0].request_id, "CONFIRM", D(100))
    assert len(state.adjustments) == 1


def test_timeout_and_no_retry() -> None:
    c, state, risk, rs, p, _ = scenario(timeout=30000)
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=30), "80")
    assert_partition(state, "650", "250", "100", "0")
    assert state.transfers[0].state == "EXPIRED" and len(state.transfers) == 1
    assert state.decisions[-1].reason == "TRANSFER_NOT_ALLOWED:NO_RETRY"


@pytest.mark.parametrize("remove", ["mark", "fee", "funding"])
def test_missing_financial_evidence_disables_decisions(remove: str) -> None:
    _, state, _, _, _, _ = scenario(remove=remove)
    assert not state.adjustments and not state.transfers
    assert state.decisions[-1].status == "NOT_AVAILABLE"
    if remove != "mark":
        assert state.partition.free_balance.value is None


def test_missing_bracket_and_stale_mark_no_transfer() -> None:
    c, _, risk, rs, p, f = scenario(enabled=False)
    assert rs.context is not None
    for altered in (replace(rs, events=()), replace(rs, context=replace(rs.context, mark=None))):
        assert altered.context is not None
        # Mark ausente debe estar ausente también del contexto financiero.
        if altered.context.mark is None:
            altered = replace(
                altered,
                context=replace(
                    altered.context, financial=replace(altered.context.financial, mark_id=None)
                ),
            )
        enabled = replace(c, auto_policy=auto())
        altered = replace(altered, evaluations=(), journal=(), last_at=None)
        st, _ = enabled.tick(enabled.initial(), risk, altered, AT)
        assert not st.transfers and st.decisions[-1].status == "NOT_AVAILABLE"
    enabled = replace(c, auto_policy=auto())
    stale_at = AT + timedelta(seconds=61)
    old = risk.context_from(p, f, stale_at)
    stale = replace(rs, context=old, evaluations=(), last_at=None)
    st, _ = enabled.tick(enabled.initial(), risk, stale, stale_at)
    assert not st.transfers


@pytest.mark.parametrize("price", ["75", "75.757575757575757575757575757575757575757575757575"])
def test_already_breached_or_zero_never_guarantees_rescue(price: str) -> None:
    _, state, _, _, _, _ = scenario(price=price)
    assert any(e.kind == "BREACH_ALREADY_OBSERVED" for e in state.journal)
    assert not state.adjustments


def test_hostile_decimal_context_exactly_same_results() -> None:
    expected = scenario()[1]
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        actual = scenario()[1]
    assert actual == expected


def test_initial_partition_mismatch_missing_or_unknown_is_not_available() -> None:
    c, _, risk, rs, _, _ = scenario(enabled=False)
    assert c.capital is not None
    for capital in (
        None,
        replace(c.capital, fill_ids=()),
        replace(
            c.capital,
            stamp=replace(
                c.capital.stamp,
                reference=replace(
                    FIXTURE,
                    provenance=Origin.UNKNOWN,
                ),
            ),
        ),
    ):
        bad = replace(c, capital=capital, auto_policy=auto())
        before = replace(rs, evaluations=(), journal=rs.journal[:-1])
        state, _ = bad.tick(bad.initial(), risk, before, AT)
        assert state.partition.status == "PARTITION_NOT_RECONCILED"
        assert state.partition.free_balance.value is None
        assert not state.transfers
    with pytest.raises(ValueError, match="PARTITION_NOT_RECONCILED"):
        replace(c.capital, free_balance_usdc=D(651))


def test_market_requires_explicit_causal_price_and_reduce_only_sell_no_open_hold() -> None:
    c, state, risk, rs, _, _ = scenario(enabled=False, market=True)
    assert state.partition.pending_order_holds.value is None
    assert rs.context is not None
    order = state.orders[0]
    price = OrderPriceEvidence(stamp("causal-price"), order.order_id, Price(D(100)), 60000)
    known = replace(state, prices=(price,))
    reserves = reservations(known, rs.context, c.reservation_policy, AT, risk.policy.leverage)
    assert reserves[-1].hold.value == D(100)
    future = replace(price, stamp=replace(price.stamp, received_at=AT + timedelta(seconds=1)))
    assert (
        reservations(
            replace(state, prices=(future,)),
            rs.context,
            c.reservation_policy,
            AT,
            risk.policy.leverage,
        )[-1].hold.value
        is None
    )
    sell = pending_order(sell=True, reduce=True)
    assert (
        reservations(
            replace(state, orders=(sell,)),
            rs.context,
            c.reservation_policy,
            AT,
            risk.policy.leverage,
        )[0].hold.value
        == 0
    )


def test_case_i_cancel_request_keeps_hold_until_terminal_reconciliation() -> None:
    c, state, risk, rs, p, _ = scenario(enabled=False)
    order = state.orders[0]
    at = AT + timedelta(seconds=1)
    state = c.cancel(state, OrderCancelRequest(order.intent, at), at)
    state, rs = advance(c, state, risk, rs, p, at, "80")
    assert_partition(state, "650", "250", "100", "0")
    terminal_at = at + timedelta(seconds=1)
    state = c.order(
        state, replace(order, status=OrderStatus.CANCELED, updated_at=terminal_at), terminal_at
    )
    state, rs = advance(c, state, risk, rs, p, terminal_at, "80")
    assert_partition(state, "750", "250", "0", "0")
    assert state.reservations[-1].cancel_requested
    assert state.reservations[-1].reconciliation == "RECONCILED"


def test_case_h_partial_fill_then_cancel_preserves_exposure_and_moves_hold_once() -> None:
    c, state, risk, rs, p, _ = scenario(enabled=False)
    assert rs.context is not None
    order = state.orders[0]
    at = AT + timedelta(seconds=1)
    partial = replace(
        order, status=OrderStatus.PARTIALLY_FILLED, filled_quantity=D(1), updated_at=at
    )
    from trading_engine.models.ids import FillId
    from trading_engine.models.orders import Fill
    from trading_engine.replay.ports import FillEvidence, ReplayFact

    fact = ReplayFact(
        at,
        FillEvidence(
            Fill(FillId(UUID(int=701)), order.order_id, at, Price(D(100)), Quantity(D(1))), partial
        ),
        2,
    )
    ledger = FinancialLedger().receive(rs.context.ledger, fact, at=at)
    state = c.order(state, partial, at)
    state, rs = advance(c, state, risk, rs, p, at, "80", ledger=ledger)
    assert_partition(state, "650", "275", "75", "0")
    assert state.reservations[-1].remaining_quantity == D(3)
    terminal_at = at + timedelta(seconds=1)
    state = c.order(
        state, replace(partial, status=OrderStatus.CANCELED, updated_at=terminal_at), terminal_at
    )
    state, rs = advance(c, state, risk, rs, p, terminal_at, "80")
    assert_partition(state, "725", "275", "0", "0")
    assert state.risk is not None and state.risk.positions[0].open_quantity == 11


def test_immutable_policy_strict_types_and_unsupported_retry() -> None:
    c, state, _, _, _, _ = scenario()
    with pytest.raises(FrozenInstanceError):
        state.__setattr__("source_cursor", 10)
    for change in (
        {"enabled": 1},
        {"request_latency_ms": True},
        {"confirmation_latency_ms": 0},
        {"retry_policy": "RETRY"},
        {"rounding_policy": "ROUND_UP"},
    ):
        with pytest.raises((ValueError, TypeError)):
            replace(c.auto_policy, **change)


def test_no_network_clients_or_commands() -> None:
    folder = Path("src/trading_engine/replay/reserve_transfer_simulation")
    forbidden = {"requests", "urllib", "socket", "http", "subprocess", "binance", "telegram", "os"}
    for path in folder.glob("*.py"):
        tree = ast.parse(path.read_text())
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any(i.split(".")[0] in forbidden for i in imports)
        assert "trading_engine.binance" not in imports


def test_contradictory_order_terminal_rejects_late_mutation() -> None:
    c, state, _, _, _, _ = scenario(enabled=False)
    order = state.orders[0]
    closed = replace(order, status=OrderStatus.REJECTED, updated_at=AT + timedelta(seconds=1))
    state = c.order(state, closed, AT + timedelta(seconds=1))
    with pytest.raises(ValueError, match="contradictorio"):
        c.order(
            state, replace(order, updated_at=AT + timedelta(seconds=1)), AT + timedelta(seconds=1)
        )


def test_unknown_source_and_missing_amount_rejected() -> None:
    command = TransferCommandEvidence(stamp("propose"), "manual", RID, "PROPOSE", D(100))
    with pytest.raises(ValueError, match="SYNTHETIC_FIXTURE"):
        FixtureReserveSource((replace(command, stamp=replace(command.stamp, reference=MODELED)),))
    with pytest.raises(ValueError, match="Importe"):
        replace(command, amount_usdc=None)


def test_closed_round_rejects_pending_contribution_and_releases_hold() -> None:
    c, state, risk, rs, p, _ = scenario()
    assert rs.context is not None
    at = AT + timedelta(seconds=60)
    sale = execution(2, Side.SELL, "10", "100", requested="10", timestamp=at)
    ledger = FinancialLedger().receive(rs.context.ledger, sale, at=at)
    state, rs = advance(c, state, risk, rs, p, at, "80", ledger=ledger)
    assert state.risk is not None and state.risk.positions[0].open_quantity == 0
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=120), "80")
    assert state.transfers[0].state == "REJECTED"
    assert state.transfers[0].rejection_reason == "ROUND_CLOSED_WITH_PENDING_TRANSFER"
    assert not state.adjustments
    assert_partition(state, "900", "0", "100", "0")


@pytest.mark.parametrize("free,available", [("650", True), ("4", False)])
def test_actual_buy_margin_above_released_market_hold_requires_extra_free(
    free: str, available: bool
) -> None:
    c, _, risk, rs, p, _ = scenario(enabled=False, free=free, market=True)
    order = c.initial_orders[0]
    reference = OrderPriceEvidence(stamp("order-price"), order.order_id, Price(D(100)), 60000)
    c = replace(c, source=FixtureReserveSource((reference,)))
    rs = replace(rs, evaluations=(), journal=rs.journal[:-1])
    state, rs = c.tick(c.initial(), risk, rs, AT)
    assert state.partition.status == "RECONCILED"
    from trading_engine.models.ids import FillId
    from trading_engine.models.orders import Fill
    from trading_engine.replay.ports import FillEvidence, ReplayFact

    assert rs.context is not None
    at = AT + timedelta(seconds=1)
    partial = replace(
        order, status=OrderStatus.PARTIALLY_FILLED, filled_quantity=D(1), updated_at=at
    )
    fill = Fill(FillId(UUID(int=703)), order.order_id, at, Price(D(120)), Quantity(D(1)))
    ledger = FinancialLedger().receive(
        rs.context.ledger, ReplayFact(at, FillEvidence(fill, partial), 3), at=at
    )
    state = c.order(state, partial, at)
    state, rs = advance(c, state, risk, rs, p, at, "80", ledger=ledger)
    assert state.risk is not None and state.risk.positions[0].initial_assigned.value == 280
    assert state.partition.pending_order_holds.value == 75
    if available:
        assert_partition(state, "645", "280", "75", "0")
    else:
        assert state.partition.status == "PARTITION_NOT_RECONCILED"
        assert state.partition.free_balance.value is None
        assert "INSUFFICIENT_FREE_BALANCE" in state.partition.free_balance.reasons[0]


def test_terminal_order_waits_for_exact_confirmed_fill_ids() -> None:
    c, state, risk, rs, _, _ = scenario(enabled=False)
    assert rs.context is not None
    order = state.orders[0]
    terminal = replace(
        order,
        status=OrderStatus.CANCELED,
        filled_quantity=D(1),
        updated_at=AT + timedelta(seconds=1),
    )
    state = c.order(state, terminal, terminal.updated_at)
    held = reservations(
        state, rs.context, c.reservation_policy, terminal.updated_at, risk.policy.leverage
    )
    assert held[-1].hold.value is None
    assert held[-1].reconciliation == "PENDING_FILL_RECONCILIATION"
    # Una cantidad acumulada no sustituye el FillId que aún no ha llegado.
    assert held[-1].filled_quantity == 0


def test_exact_zero_buffer_is_recorded_before_any_addition() -> None:
    c, _, risk, rs, _, _ = scenario(enabled=False)
    assert rs.context is not None and c.capital is not None
    p = FinancialPerformance(policy(capital="1000"))
    # BUY 10@100, fees explícitas 42, Mark80: wallet aislado208, margen8, MMR8.
    financial = complete(p, rs.context.ledger, price="80", amounts=("42",))
    risk_state = replace(
        rs, evaluations=(), journal=(), context=risk.context_from(p, financial, AT)
    )
    capital = replace(c.capital, account_wallet_usdc=D(958), isolated_by_round=((RID, D(208)),))
    enabled = replace(c, capital=capital, auto_policy=auto())
    state, _ = enabled.tick(enabled.initial(), risk, risk_state, AT)
    assert state.risk is not None and state.risk.positions[0].buffer.value == 0
    assert any(e.kind == "BREACH_ALREADY_OBSERVED" for e in state.journal)
    assert not state.adjustments


def test_fees_and_funding_allocation_consumed_once() -> None:
    c, _, risk, rs, _, _ = scenario(enabled=False)
    assert rs.context is not None and c.capital is not None
    from trading_engine.replay.financial_performance.evidence import FundingEvidence

    p = FinancialPerformance(policy())
    funding = FundingEvidence("funding-debit", SYMBOL, AT, RID, D(10), FIXTURE, amount_usdc=D(-3))
    financial = complete(p, rs.context.ledger, price="80", amounts=("2",), funding=funding)
    risk_state = replace(
        rs, evaluations=(), journal=(), context=risk.context_from(p, financial, AT)
    )
    capital = replace(c.capital, account_wallet_usdc=D(995), isolated_by_round=((RID, D(245)),))
    c = replace(c, capital=capital)
    state, _ = c.tick(c.initial(), risk, risk_state, AT)
    assert_partition(state, "650", "245", "100", "0", "995")
    assert state.risk is not None
    assert state.risk.account_wallet.value == 995


def test_future_fixture_rejection_does_not_change_causal_request_id() -> None:
    c, original, risk, rs, p, _ = scenario()
    request_id = original.transfers[0].request_id
    rejection_at = AT + timedelta(seconds=30)
    command = TransferCommandEvidence(
        stamp("reject-later", at=rejection_at),
        request_id,
        RID,
        "REJECT",
        reason="SYNTHETIC_REJECTION",
    )
    with_rejection = replace(c, source=FixtureReserveSource((command,)))
    before = replace(
        rs,
        evaluations=(),
        journal=rs.journal[:-1],
        events=tuple(e for e in rs.events if not hasattr(e, "request_id")),
    )
    # rs aún sólo contiene schedule; la solicitud original sigue pendiente.
    state, updated = with_rejection.tick(with_rejection.initial(), risk, before, AT)
    assert state.transfers[0].request_id == request_id
    assert state.source_cursor == 0
    state, _ = advance(with_rejection, state, risk, updated, p, rejection_at, "80")
    assert state.transfers[0].state == "REJECTED"
    assert not state.adjustments and not state.queue
    assert_partition(state, "650", "250", "100", "0")


def test_positive_send_latency_has_separate_pending_states() -> None:
    c, _, risk, rs, p, _ = scenario()
    c = replace(c, auto_policy=replace(c.auto_policy, request_latency_ms=10000))
    before = replace(rs, evaluations=(), journal=rs.journal[:-1])
    state, rs = c.tick(c.initial(), risk, before, AT)
    assert state.transfers[0].state == "REQUEST_PENDING" and state.transfers[0].attempts == 0
    state, rs = advance(c, state, risk, rs, p, AT + timedelta(seconds=10), "80")
    assert state.transfers[0].state == "CONFIRMATION_PENDING" and state.transfers[0].attempts == 1
    assert not state.adjustments


def test_retrospective_and_foreign_controller_contexts_rejected() -> None:
    c, state, risk, rs, _, _ = scenario()
    with pytest.raises(ValueError, match="retrospectivo"):
        c.tick(state, risk, rs, AT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="incompatible"):
        c.tick(replace(state, identity="foreign"), risk, rs, AT)
    assert state.risk is not None
    bad = c.propose(
        state, AT + timedelta(seconds=1), state.risk.positions[0], D(100), "stale", risk
    )
    assert bad.transfers[-1].rejection_reason == "TRANSFER_NOT_ALLOWED:NONCAUSAL_POSITION"


def test_unrepresentable_latency_and_foreign_initial_order_fail_closed() -> None:
    c, state, risk, _, _, _ = scenario(enabled=False)
    invalid_factories: tuple[Callable[[], AutoMarginPolicy], ...] = (
        lambda: replace(c.auto_policy, request_latency_ms=10**100),
        lambda: replace(c.auto_policy, confirmation_latency_ms=10**100),
        lambda: replace(c.auto_policy, timeout_ms=10**100),
    )
    for make_invalid_policy in invalid_factories:
        with pytest.raises(ValueError, match="Latencia"):
            make_invalid_policy()
    bad_order = replace(
        c.initial_orders[0],
        intent=replace(
            c.initial_orders[0].intent,
            symbol=Symbol("BTCUSDC"),
        ),
    )
    with pytest.raises(ValueError, match="otro símbolo"):
        replace(c, initial_orders=(bad_order,))
    assert state.risk is not None
    with pytest.raises(ValueError, match="rango del reloj"):
        c.propose(
            state,
            datetime.max.replace(tzinfo=AT.tzinfo),
            state.risk.positions[0],
            D(100),
            "too-late",
            risk,
        )


def test_initial_partition_mismatch_preserves_actual_wallet_not_declared_balance() -> None:
    c, _, risk, rs, _, _ = scenario(enabled=False)
    assert c.capital is not None
    # Declaración de1500 contra wallet confirmado1000; ninguno se acredita como libre.
    wrong_capital = replace(c.capital, account_wallet_usdc=D(1500), free_balance_usdc=D(1150))
    c = replace(c, capital=wrong_capital)
    before = replace(rs, evaluations=(), journal=rs.journal[:-1])
    state, _ = c.tick(c.initial(), risk, before, AT)
    assert state.partition.account_wallet.value == 1000
    assert state.partition.free_balance.value is None
    assert state.partition.status == "PARTITION_NOT_RECONCILED"
