from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from trading_engine.models.orders import Order, OrderIntent


def test_intent_is_immediate_and_has_only_creation_time(intent: OrderIntent) -> None:
    assert "not_before" not in {item.name for item in fields(OrderIntent)}
    assert not hasattr(intent, "not_before")
    assert not hasattr(intent, "timestamp")
    assert isinstance(intent.created_at, datetime)
    with pytest.raises(TypeError):
        replace(intent, not_before=intent.created_at)  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        intent.created_at = intent.created_at + timedelta(seconds=1)  # type: ignore[misc]


@pytest.mark.parametrize(
    "value, error",
    [
        (datetime(2026, 1, 1), ValueError),
        (datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-6))), ValueError),
        ("2026-01-01", TypeError),
        (None, TypeError),
    ],
)
def test_intent_requires_utc_creation_time(
    intent: OrderIntent, value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(intent, created_at=value)  # type: ignore[arg-type]


def test_order_cannot_be_accepted_before_intent_creation(order: Order) -> None:
    later = replace(order.intent, created_at=order.intent.created_at + timedelta(seconds=37))
    with pytest.raises(ValueError):
        replace(order, intent=later)
    with pytest.raises(ValueError):
        replace(
            order,
            intent=later,
            created_at=later.created_at - timedelta(microseconds=1),
            updated_at=later.created_at,
        )
    accepted = replace(
        order, intent=later, created_at=later.created_at, updated_at=later.created_at
    )
    assert accepted.created_at == later.created_at
    assert replace(
        accepted,
        created_at=later.created_at + timedelta(seconds=1),
        updated_at=later.created_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError):
        replace(accepted, updated_at=accepted.created_at - timedelta(microseconds=1))
