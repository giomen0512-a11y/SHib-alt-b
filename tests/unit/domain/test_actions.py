from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import pytest

from trading_engine.models.actions import ScheduledAction
from trading_engine.models.ids import ActionId


@pytest.mark.parametrize("seconds", [0, 1, 37, 86400])
def test_action_accepts_explicit_immediate_or_deferred_times(
    action: ScheduledAction, seconds: int
) -> None:
    result = replace(action, not_before=action.created_at + timedelta(seconds=seconds))
    assert result.not_before >= result.created_at
    assert result.action_id == action.action_id
    assert not hasattr(result, "quantity")
    assert not hasattr(result, "metadata")


def test_action_rejects_eligibility_before_creation(action: ScheduledAction) -> None:
    with pytest.raises(ValueError, match="not_before"):
        replace(action, not_before=action.created_at - timedelta(microseconds=1))


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
def test_action_requires_utc_datetimes(
    action: ScheduledAction, field: str, value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(action, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"action_id": "id"}, TypeError),
        ({"action_id": None}, TypeError),
        ({"round_id": "id"}, TypeError),
        ({"round_id": None}, TypeError),
        ({"symbol": "SHIBUSDC"}, TypeError),
        ({"symbol": None}, TypeError),
        ({"kind": ""}, ValueError),
        ({"kind": " \t"}, ValueError),
        ({"kind": 1}, TypeError),
        ({"kind": None}, TypeError),
    ],
)
def test_action_rejects_invalid_fields(
    action: ScheduledAction, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(action, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", [item.name for item in fields(ScheduledAction)])
def test_action_is_frozen(action: ScheduledAction, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(action, field, None)


def test_action_requires_caller_supplied_id(action: ScheduledAction) -> None:
    identifier = ActionId(UUID(int=987))
    assert replace(action, action_id=identifier).action_id is identifier
    with pytest.raises(TypeError):
        ScheduledAction(  # type: ignore[call-arg]
            round_id=action.round_id,
            symbol=action.symbol,
            kind=action.kind,
            created_at=action.created_at,
            not_before=action.not_before,
        )
    with pytest.raises(TypeError):
        replace(action, action_id=cast(ActionId, None))
