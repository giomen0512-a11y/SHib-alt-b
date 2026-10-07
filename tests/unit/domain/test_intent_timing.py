from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from trading_engine.models.orders import Order, OrderIntent


@pytest.mark.parametrize("seconds", [0, 1, 120, 86400])
def test_intent_accepts_explicit_immediate_or_deferred_times(
    intent: OrderIntent, seconds: int
) -> None:
    result = replace(intent, not_before=intent.created_at + timedelta(seconds=seconds))
    assert result.not_before >= result.created_at
    assert not hasattr(result, "timestamp")
    with pytest.raises(FrozenInstanceError):
        result.not_before = result.created_at  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.created_at = result.not_before  # type: ignore[misc]


def test_intent_rejects_eligibility_before_creation(intent: OrderIntent) -> None:
    with pytest.raises(ValueError, match="not_before"):
        replace(intent, not_before=intent.created_at - timedelta(microseconds=1))


@pytest.mark.parametrize("field", ["created_at", "not_before"])
@pytest.mark.parametrize(
    "value, error",
    [
        (datetime(2026, 1, 1), ValueError),
        (datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-6))), ValueError),
        ("2026-01-01", TypeError),
        (None, TypeError),
    ],
)
def test_intent_requires_utc_datetimes(
    intent: OrderIntent, field: str, value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(intent, **{field: value})  # type: ignore[arg-type]


def test_order_cannot_be_accepted_before_deferred_eligibility(order: Order) -> None:
    deferred = replace(order.intent, not_before=order.intent.created_at + timedelta(seconds=37))
    with pytest.raises(ValueError):
        replace(order, intent=deferred)
    with pytest.raises(ValueError):
        replace(
            order,
            intent=deferred,
            created_at=deferred.not_before - timedelta(microseconds=1),
            updated_at=deferred.not_before,
        )
    accepted = replace(
        order, intent=deferred, created_at=deferred.not_before, updated_at=deferred.not_before
    )
    assert accepted.created_at == deferred.not_before
    assert replace(
        accepted,
        created_at=deferred.not_before + timedelta(seconds=1),
        updated_at=deferred.not_before + timedelta(seconds=1),
    )
    with pytest.raises(ValueError):
        replace(accepted, updated_at=accepted.created_at - timedelta(microseconds=1))
