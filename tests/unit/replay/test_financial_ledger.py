"""Contabilidad exacta y bloqueos causales sobre hechos, sin estrategia duplicada."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from trading_engine.models.enums import OrderStatus, OrderType, Side
from trading_engine.models.ids import FillId, IntentId, OrderId, RoundId
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.values import Price, Quantity, Symbol
from trading_engine.replay.financial_ledger import FinancialLedger, LedgerPolicy
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_ledger.policy import ExecutionOrigin, ledger_context
from trading_engine.replay.ports import FillEvidence, ReplayFact

from .conftest import DUE, START, SYMBOL


def execution(
    number: int,
    side: Side,
    quantity: str,
    price: str,
    *,
    order_number: int | None = None,
    round_number: int = 100,
    requested: str = "100",
    cumulative: str | None = None,
    timestamp: datetime | None = None,
    received_at: datetime | None = None,
    origin: str = "SYNTHETIC_FIXTURE",
) -> ReplayFact:
    oid = number if order_number is None else order_number
    timestamp = DUE + timedelta(seconds=number) if timestamp is None else timestamp
    intent = OrderIntent(
        IntentId(UUID(int=oid + 1000)),
        RoundId(UUID(int=round_number)),
        SYMBOL,
        side,
        OrderType.MARKET if side is Side.BUY else OrderType.LIMIT,
        Quantity(Decimal(requested)),
        START,
        None if side is Side.BUY else Price(Decimal("999")),
        side is Side.SELL,
    )
    total = Decimal(quantity if cumulative is None else cumulative)
    status = OrderStatus.FILLED if total == intent.quantity.value else OrderStatus.PARTIALLY_FILLED
    order = Order(OrderId(UUID(int=oid)), intent, status, total, START, timestamp)
    fill = Fill(
        FillId(UUID(int=number)),
        order.order_id,
        timestamp,
        Price(Decimal(price)),
        Quantity(Decimal(quantity)),
    )
    return ReplayFact(
        timestamp if received_at is None else received_at, FillEvidence(fill, order), number, origin
    )


def apply(ledger: FinancialLedger, *facts: ReplayFact) -> LedgerState:
    state = ledger.initial()
    for fact in facts:
        state = ledger.receive(state, fact, at=fact.received_at)
        assert not state.conflicts
    ledger.validate(state, facts[-1].received_at)
    return state


@pytest.mark.parametrize(
    ("trades", "pnl", "quantity", "cost", "average"),
    [
        (((Side.BUY, "10", "100"), (Side.SELL, "10", "101")), "10", "0", "0", None),
        (
            ((Side.BUY, "10", "100"), (Side.BUY, "10", "90"), (Side.SELL, "10", "97")),
            "20",
            "10",
            "950",
            "95",
        ),
        (
            ((Side.BUY, "10", "100"), (Side.SELL, "5", "110"), (Side.BUY, "5", "80")),
            "50",
            "10",
            "900",
            "90",
        ),
        (((Side.BUY, "10", "100"),), "0", "10", "1000", "100"),
        (((Side.BUY, "10", "100"), (Side.SELL, "10", "90")), "-100", "0", "0", None),
    ],
)
def test_exact_moving_average_cases(
    trades: tuple[tuple[Side, str, str], ...],
    pnl: str,
    quantity: str,
    cost: str,
    average: str | None,
) -> None:
    ledger = FinancialLedger()
    state = apply(ledger, *(execution(n, *trade) for n, trade in enumerate(trades, 1)))
    report = ledger.report(state)
    position = report.round_ledger[0]
    assert report.gross_realized_pnl_usdc == Decimal(pnl)
    assert report.open_quantity == Decimal(quantity) and report.open_cost_basis_usdc == Decimal(
        cost
    )
    assert position.average_open_cost_usdc == (None if average is None else Decimal(average))
    assert (
        position.closed_at is None
    )  # Terminar el intervalo o vender no declara CLOSED estratégico.
    assert all(e.notional_usdc == e.quantity * e.price for e in state.entries)
    assert (
        report.net_realized_pnl
        == report.fees
        == report.funding
        == report.account_equity
        == "NOT_AVAILABLE"
    )
    assert report.unrealized_pnl == report.drawdown == report.liquidation_price == "NOT_AVAILABLE"


def test_partials_cancel_and_replacement_do_not_reverse_or_duplicate_economy() -> None:
    ledger = FinancialLedger()
    buy = execution(1, Side.BUY, "10", "100")
    old = execution(2, Side.SELL, "4", "101", requested="10")
    state = apply(ledger, buy, old)
    assert isinstance(old.payload, FillEvidence)
    terminal = replace(
        old.payload.order, status=OrderStatus.CANCELED, updated_at=DUE + timedelta(seconds=3)
    )
    repeated = ReplayFact(terminal.updated_at, FillEvidence(old.payload.fill, terminal), 3)
    assert ledger.receive(state, repeated, at=repeated.received_at) == state
    new = execution(4, Side.SELL, "6", "102")
    state = ledger.receive(state, new, at=new.received_at)
    assert not state.conflicts and len(state.entries) == 3
    assert ledger.report(state).gross_realized_pnl_usdc == Decimal("16")
    assert ledger.report(state).gross_sell_notional_usdc == Decimal("1016")
    assert ledger.report(state).open_quantity == 0
    closed = ledger.reconcile_closed(state, (RoundId(UUID(int=100)),), at=new.received_at)
    assert closed.round_ledger[0].closed_at == new.received_at
    ledger.validate(closed, new.received_at)


def test_multiple_fills_one_order_shortfill_canceled_retains_cost() -> None:
    ledger = FinancialLedger()
    first = execution(1, Side.BUY, "2", "100", requested="8")
    second = execution(2, Side.BUY, "3", "90", requested="8", order_number=1, cumulative="5")
    state = apply(ledger, first, second)
    assert isinstance(second.payload, FillEvidence)
    terminal = replace(
        second.payload.order, status=OrderStatus.CANCELED, updated_at=DUE + timedelta(seconds=3)
    )
    repeat = ReplayFact(terminal.updated_at, FillEvidence(second.payload.fill, terminal), 3)
    assert ledger.receive(state, repeat, at=repeat.received_at) == state
    assert state.round_ledger[0].open_quantity == Decimal("5")
    assert state.round_ledger[0].open_cost_basis_usdc == Decimal("470")


@pytest.mark.parametrize(
    "damage", ["price", "quantity", "timestamp", "round", "side", "symbol", "order", "origin"]
)
def test_conflicting_fill_id_is_blocked_without_overwriting(damage: str) -> None:
    ledger = FinancialLedger()
    fact = execution(1, Side.BUY, "10", "100")
    state = apply(ledger, fact)
    assert isinstance(fact.payload, FillEvidence)
    evidence = fact.payload
    fill, order = evidence.fill, evidence.order
    if damage == "price":
        fill = replace(fill, price=Price(Decimal("101")))
    elif damage == "quantity":
        fill = replace(fill, quantity=Quantity(Decimal("9")))
    elif damage == "timestamp":
        fill = replace(fill, timestamp=fill.timestamp + timedelta(milliseconds=1))
    elif damage == "round":
        order = replace(order, intent=replace(order.intent, round_id=RoundId(UUID(int=200))))
    elif damage == "side":
        order = replace(order, intent=replace(order.intent, side=Side.SELL))
    elif damage == "symbol":
        order = replace(order, intent=replace(order.intent, symbol=Symbol("SHIBUSDC")))
    elif damage == "order":
        order = replace(order, order_id=OrderId(UUID(int=200)))
        fill = replace(fill, order_id=order.order_id)
    received = DUE + timedelta(seconds=2)
    changed = ReplayFact(
        received,
        FillEvidence(fill, order),
        2,
        "MODELED_EXECUTION" if damage == "origin" else fact.provenance,
    )
    blocked = ledger.receive(state, changed, at=received)
    assert blocked.conflicts and blocked.entries == state.entries
    assert blocked.round_ledger[0].open_quantity == state.round_ledger[0].open_quantity
    ledger.validate(blocked, received)


@pytest.mark.parametrize(
    "cause",
    [
        "oversell",
        "overfill",
        "binding",
        "late",
        "tie",
        "after_close",
        "closed_exposure",
        "unknown_close",
    ],
)
def test_inconsistent_evidence_blocks_financial_prefix(cause: str) -> None:
    ledger = FinancialLedger()
    state = apply(ledger, execution(1, Side.BUY, "10", "100"))
    at = DUE + timedelta(seconds=3)
    if cause in {"closed_exposure", "unknown_close"}:
        rid = 100 if cause == "closed_exposure" else 200
        blocked = ledger.reconcile_closed(state, (RoundId(UUID(int=rid)),), at=at)
    else:
        fact = execution(3, Side.SELL, "11" if cause == "oversell" else "1", "101")
        if cause == "overfill":
            state = apply(ledger, execution(1, Side.BUY, "5", "100", requested="8"))
            fact = execution(3, Side.BUY, "4", "100", requested="8", order_number=1, cumulative="4")
        elif cause == "binding":
            fact = execution(3, Side.SELL, "1", "101", order_number=1)
        elif cause == "late":
            fact = execution(3, Side.BUY, "1", "80", timestamp=DUE, received_at=at)
        elif cause == "tie":
            fact = execution(
                2, Side.SELL, "1", "101", timestamp=DUE + timedelta(seconds=1), received_at=at
            )
            fact = replace(fact, sequence=0)
        elif cause == "after_close":
            sale = execution(2, Side.SELL, "10", "101")
            state = ledger.receive(state, sale, at=sale.received_at)
            state = ledger.reconcile_closed(state, (RoundId(UUID(int=100)),), at=sale.received_at)
            fact = execution(3, Side.BUY, "1", "100")
        blocked = ledger.receive(state, fact, at=at)
    assert blocked.conflicts and blocked.entries == state.entries
    assert (
        ledger.report(blocked).gross_realized_pnl_usdc
        == ledger.report(state).gross_realized_pnl_usdc
    )
    assert (
        ledger.receive(blocked, execution(4, Side.BUY, "1", "1"), at=DUE + timedelta(seconds=4))
        == blocked
    )
    ledger.validate(blocked, at)


def test_same_timestamp_ties_follow_explicit_sequence_without_sorting_history() -> None:
    ledger = FinancialLedger()
    at = DUE + timedelta(seconds=1)
    state = apply(
        ledger,
        execution(1, Side.BUY, "10", "100"),
        execution(2, Side.SELL, "5", "110", timestamp=at),
        execution(3, Side.BUY, "5", "80", timestamp=at),
    )
    assert state.round_ledger[0].average_open_cost_usdc == Decimal("90")
    assert state.round_ledger[0].gross_realized_pnl_usdc == Decimal("50")


def test_future_delivery_is_not_recognized_or_retained_as_execution() -> None:
    ledger = FinancialLedger()
    fact = execution(1, Side.BUY, "10", "100", received_at=DUE + timedelta(seconds=10))
    state = ledger.receive(ledger.initial(), fact, at=DUE)
    assert state.conflicts and not state.entries and not state.journal
    assert state.conflicts[0].observation is None
    ledger.validate(state, DUE)


@pytest.mark.parametrize("payload", ["order", "intent"])
def test_order_and_intent_are_not_economic_entries(payload: str) -> None:
    ledger = FinancialLedger()
    fact = execution(1, Side.BUY, "10", "100")
    assert isinstance(fact.payload, FillEvidence)
    if payload == "order":
        state = ledger.receive(
            ledger.initial(), replace(fact, payload=fact.payload.order), at=fact.received_at
        )
        assert state.conflicts and not state.entries
    else:
        with pytest.raises(TypeError):
            ReplayFact(fact.received_at, fact.payload.order.intent, 0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "origin", ["MODELED_EXECUTION", "SYNTHETIC_FIXTURE", "REAL_OBSERVED", "UNKNOWN"]
)
def test_provenance_is_explicit_and_observed_mode_not_enabled(origin: str) -> None:
    ledger = FinancialLedger()
    fact = execution(1, Side.BUY, "1", "100", origin=origin)
    state = ledger.receive(ledger.initial(), fact, at=fact.received_at)
    assert bool(state.conflicts) == (origin in {"REAL_OBSERVED", "UNKNOWN"})
    if not state.conflicts:
        assert state.entries[0].provenance.value == origin
        assert ledger.report(state).modeled_results == (origin == "MODELED_EXECUTION")


def test_decimal_is_independent_and_final_sale_releases_division_residual() -> None:
    ledger = FinancialLedger()
    trades = (
        execution(1, Side.BUY, "1", "1"),
        execution(2, Side.BUY, "2", "2"),
        execution(3, Side.SELL, "1", "3"),
        execution(4, Side.SELL, "2", "3"),
    )
    expected = apply(ledger, *trades)
    with localcontext() as hostile:
        hostile.prec = 3
        hostile.rounding = ROUND_DOWN
        hostile.Emax = 9
        hostile.traps[Inexact] = True
        flags = hostile.flags.copy()
        actual = apply(ledger, *trades)
        assert ledger.report(actual) == ledger.report(expected)
        assert hostile.prec == 3 and hostile.rounding == ROUND_DOWN and hostile.flags == flags
    assert actual.round_ledger[0].open_quantity == actual.round_ledger[0].open_cost_basis_usdc == 0
    assert actual.round_ledger[0].gross_realized_pnl_usdc == Decimal("4")
    with localcontext(ledger_context()):
        assert sum((e.cost_released_usdc for e in actual.entries), Decimal("0")) == Decimal("5")


def test_rounds_are_separate_and_no_quantity_scale_or_leverage_is_applied() -> None:
    ledger = FinancialLedger()
    state = apply(
        ledger,
        execution(1, Side.BUY, "10", "100"),
        execution(2, Side.BUY, "5", "80", round_number=200),
        execution(3, Side.SELL, "10", "101"),
    )
    assert len(state.round_ledger) == 2
    assert state.round_ledger[0].open_quantity == 0
    assert state.round_ledger[1].open_quantity == Decimal("5")
    assert ledger.report(state).open_cost_basis_usdc == Decimal("400")
    assert ledger.report(state).gross_buy_notional_usdc == Decimal("1400")


@pytest.mark.parametrize("damage", ["cost", "entry", "journal", "fill_ids", "identity"])
def test_snapshot_recomputes_all_accounting_and_rejects_tampering(damage: str) -> None:
    ledger = FinancialLedger()
    state = apply(ledger, execution(1, Side.BUY, "10", "100"))
    if damage == "cost":
        state = replace(
            state, round_ledger=(replace(state.round_ledger[0], open_cost_basis_usdc=Decimal("1")),)
        )
    elif damage == "entry":
        state = replace(state, entries=(replace(state.entries[0], notional_usdc=Decimal("1")),))
    elif damage == "journal":
        state = replace(state, journal=())
    elif damage == "fill_ids":
        state = replace(state, entries=state.entries + state.entries)
    else:
        state = replace(state, identity="other")
    with pytest.raises(ValueError):
        ledger.validate(state, DUE + timedelta(seconds=1))


@pytest.mark.parametrize(
    "bad", [datetime(2026, 1, 2), datetime(2026, 1, 2, tzinfo=UTC) - timedelta(days=1)]
)
def test_invalid_or_backwards_accounting_clock(bad: datetime) -> None:
    ledger = FinancialLedger()
    state = apply(ledger, execution(1, Side.BUY, "1", "100"))
    with pytest.raises(ValueError):
        ledger.validate(state, bad)


def test_policy_identity_and_unsupported_contracts() -> None:
    policy = LedgerPolicy()
    assert (
        policy.identity
        == replace(policy, allowed_origins=tuple(reversed(policy.allowed_origins))).identity
    )
    assert policy.identity != replace(policy, allowed_origins=(ExecutionOrigin.MODELED,)).identity
    with pytest.raises(ValueError):
        replace(policy, rules_version="future")
    with pytest.raises(ValueError):
        replace(policy, symbol=Symbol("SHIBUSDC"))
    with pytest.raises(ValueError):
        replace(policy, allowed_origins=(ExecutionOrigin.OBSERVED,))


@pytest.mark.parametrize("bad", [1.0, 1, True, "1"])
def test_non_decimal_financial_inputs_rejected(bad: object) -> None:
    with pytest.raises(TypeError):
        Quantity(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Price(bad)  # type: ignore[arg-type]


def test_order_id_mismatch_rejected_at_domain_boundary() -> None:
    fact = execution(1, Side.BUY, "1", "100")
    assert isinstance(fact.payload, FillEvidence)
    with pytest.raises(ValueError):
        FillEvidence(replace(fact.payload.fill, order_id=OrderId(UUID(int=20))), fact.payload.order)


def test_nonrepresentable_notional_blocks_without_creating_entry() -> None:
    ledger = FinancialLedger()
    fact = execution(1, Side.BUY, "10", "1e999999999999999999")
    state = ledger.receive(ledger.initial(), fact, at=fact.received_at)
    assert state.conflicts and state.conflicts[0].reason.startswith("Overflow:")
    assert not state.entries
    ledger.validate(state, fact.received_at)


def test_rounding_of_reordered_buy_totals_never_fabricates_realized_profit() -> None:
    ledger = FinancialLedger()
    state = apply(
        ledger,
        execution(3, Side.BUY, "1", "1e50", timestamp=DUE + timedelta(seconds=1)),
        execution(1, Side.BUY, "1", "6", timestamp=DUE + timedelta(seconds=2)),
        execution(2, Side.BUY, "1", "6", timestamp=DUE + timedelta(seconds=3)),
    )
    position = state.round_ledger[0]
    assert position.gross_realized_pnl_usdc == 0
    assert position.open_cost_basis_usdc == position.gross_buy_notional_usdc
    assert all(e.gross_realized_pnl_usdc == 0 for e in state.entries)


def test_notional_underflow_blocks_without_fabricating_zero_cost() -> None:
    ledger = FinancialLedger()
    fact = execution(1, Side.BUY, "1e-999999999999999999", "1e-999999999999999999")
    state = ledger.receive(ledger.initial(), fact, at=fact.received_at)
    assert state.conflicts and "Nocional positivo no representable" in state.conflicts[0].reason
    assert not state.entries and not state.round_ledger
    ledger.validate(state, fact.received_at)
