from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trading_engine.models.account import BalanceSnapshot, Position
from trading_engine.models.decisions import RiskDecision
from trading_engine.models.enums import PositionSide, RoundStatus
from trading_engine.models.rounds import Round
from trading_engine.models.values import Money


def test_position_side_is_separate_from_positive_quantity(position: Position) -> None:
    short = replace(position, side=PositionSide.SHORT)
    assert short.quantity == position.quantity
    assert short.average_entry_price == position.average_entry_price
    with pytest.raises(FrozenInstanceError):
        position.quantity = position.quantity  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"quantity": 1.0}, TypeError),
        ({"quantity": Decimal("0")}, TypeError),
        ({"average_entry_price": 1.0}, TypeError),
        ({"average_entry_price": Decimal("0")}, TypeError),
        ({"side": "LONG"}, TypeError),
        ({"symbol": "SHIBUSDC"}, TypeError),
        ({"timestamp": datetime(2026, 1, 1)}, ValueError),
    ],
)
def test_position_rejects_invalid_fields(
    position: Position, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(position, **changes)  # type: ignore[arg-type]


def test_balance_zero_and_same_currency(balance: BalanceSnapshot) -> None:
    zero = Money(Decimal("0"), "usdc")
    assert replace(balance, wallet_balance=zero, available_balance=zero).wallet_balance == zero
    # No se impone available <= wallet: su relación depende del futuro proveedor.
    assert replace(balance, available_balance=Money(Decimal("101"), "USDC"))
    with pytest.raises(FrozenInstanceError):
        balance.available_balance = zero  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"wallet_balance": Money(Decimal("-1"), "USDC")}, ValueError),
        ({"available_balance": Money(Decimal("-1"), "USDC")}, ValueError),
        ({"available_balance": Money(Decimal("1"), "USD")}, ValueError),
        ({"wallet_balance": 1.0}, TypeError),
        ({"available_balance": 1.0}, TypeError),
        ({"timestamp": datetime(2026, 1, 1)}, ValueError),
    ],
)
def test_balance_rejects_invalid_fields(
    balance: BalanceSnapshot, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(balance, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", list(RoundStatus))
def test_round_valid_snapshots(round_state: Round, status: RoundStatus) -> None:
    opened = round_state.created_at + timedelta(seconds=1)
    closed = opened + timedelta(seconds=1)
    result = replace(
        round_state,
        status=status,
        opened_at=opened
        if status in (RoundStatus.OPEN, RoundStatus.CLOSING, RoundStatus.CLOSED)
        else None,
        closed_at=closed if status is RoundStatus.CLOSED else None,
    )
    assert result.status is status
    with pytest.raises(FrozenInstanceError):
        result.status = RoundStatus.FAILED  # type: ignore[misc]


@pytest.mark.parametrize("status", [RoundStatus.OPEN, RoundStatus.CLOSING, RoundStatus.CLOSED])
def test_round_requires_open_time(round_state: Round, status: RoundStatus) -> None:
    with pytest.raises(ValueError):
        replace(round_state, status=status)


def test_round_closed_requires_close_time(round_state: Round) -> None:
    with pytest.raises(ValueError):
        replace(round_state, status=RoundStatus.CLOSED, opened_at=round_state.created_at)


@pytest.mark.parametrize(
    "status", [RoundStatus.PENDING_ENTRY, RoundStatus.OPEN, RoundStatus.CLOSING]
)
def test_round_active_states_reject_close_time(round_state: Round, status: RoundStatus) -> None:
    with pytest.raises(ValueError):
        replace(
            round_state,
            status=status,
            opened_at=round_state.created_at,
            closed_at=round_state.created_at,
        )


def test_pending_round_rejects_open_time(round_state: Round) -> None:
    with pytest.raises(ValueError):
        replace(round_state, opened_at=round_state.created_at)


@pytest.mark.parametrize("field", ["opened_at", "closed_at"])
def test_round_times_cannot_precede_creation(round_state: Round, field: str) -> None:
    with pytest.raises(ValueError):
        replace(
            round_state,
            status=RoundStatus.FAILED,
            **{field: round_state.created_at - timedelta(seconds=1)},  # type: ignore[arg-type]
        )


def test_round_close_cannot_precede_open(round_state: Round) -> None:
    with pytest.raises(ValueError):
        replace(
            round_state,
            status=RoundStatus.CLOSED,
            opened_at=round_state.created_at + timedelta(seconds=1),
            closed_at=round_state.created_at,
        )
    assert replace(
        round_state,
        status=RoundStatus.CLOSED,
        opened_at=round_state.created_at,
        closed_at=round_state.created_at,
    )


@pytest.mark.parametrize("field", ["created_at", "opened_at", "closed_at"])
def test_round_requires_aware_times(round_state: Round, field: str) -> None:
    with pytest.raises(ValueError):
        replace(round_state, status=RoundStatus.FAILED, **{field: datetime(2026, 1, 1)})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes", [{"round_id": "id"}, {"status": "OPEN"}, {"symbol": "SHIBUSDC"}]
)
def test_round_requires_domain_types(round_state: Round, changes: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        replace(round_state, **changes)  # type: ignore[arg-type]


def test_failed_round_can_describe_failure_before_or_after_entry(round_state: Round) -> None:
    assert replace(round_state, status=RoundStatus.FAILED)
    assert replace(
        round_state,
        status=RoundStatus.FAILED,
        opened_at=round_state.created_at,
        closed_at=round_state.created_at,
    )


def test_risk_result_has_explicit_rejection_reason() -> None:
    assert RiskDecision(True).approved
    rejection = RiskDecision(False, "Rejected by test fake")
    assert not rejection.approved and rejection.reason
    with pytest.raises(ValueError):
        RiskDecision(False)
    with pytest.raises(ValueError):
        RiskDecision(False, " ")
    with pytest.raises(TypeError):
        replace(rejection, approved=1)  # type: ignore[arg-type]
