from collections.abc import Callable
from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import pytest

import trading_engine.models.identity as identity_module
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.identity import (
    IDENTITY_SCHEMA_VERSION,
    PROJECT_NAMESPACE,
    make_action_id,
    make_intent_id,
    make_round_id,
    make_signal_id,
)
from trading_engine.models.ids import ActionId, IntentId, RoundId, SignalId
from trading_engine.models.orders import OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.signals import Signal
from trading_engine.models.values import Symbol

CHECKPOINT = datetime(2026, 10, 8, 14, 0, 0, 123456, tzinfo=UTC)
ELIGIBLE = CHECKPOINT + timedelta(seconds=37)
SYMBOL = Symbol("testusdc")
SIGNAL = SignalId(UUID("b646573c-50f1-51fa-b826-244806b65abc"))
ROUND = RoundId(UUID("bfe766e7-2758-53b8-9ea0-31603992707b"))
ACTION = ActionId(UUID("b76f3ed8-1bf5-51ed-bc0c-5798381a0ded"))
INTENT = IntentId(UUID("c32f10fd-3bf3-58cb-a770-29440deb8d35"))


def test_frozen_identity_golden_vectors() -> None:
    assert PROJECT_NAMESPACE == UUID("22842300-09df-530e-93ea-1e985518f6d1")
    assert IDENTITY_SCHEMA_VERSION == "1"
    signal: SignalId = make_signal_id("strategy.example", SYMBOL, CHECKPOINT, "observation")
    round_id: RoundId = make_round_id(SIGNAL)
    action: ActionId = make_action_id(ROUND, "reevaluate", CHECKPOINT, ELIGIBLE)
    intent: IntentId = make_intent_id(ROUND, "proposal", ELIGIBLE, str(ACTION))
    assert signal == SIGNAL
    assert round_id == ROUND
    assert action == ACTION
    assert intent == INTENT
    assert len({signal, round_id, action, intent}) == 4
    assert all(identifier.version == 5 for identifier in (signal, round_id, action, intent))


def test_timestamp_golden_always_has_six_microsecond_digits() -> None:
    assert make_signal_id(
        "strategy.example", SYMBOL, CHECKPOINT.replace(microsecond=0), "observation"
    ) == UUID("00fec0eb-44cc-573e-8114-09526a01e762")


@pytest.mark.parametrize("entity", ["signal", "round", "action", "intent"])
def test_one_hundred_identical_calls_preserve_identity(entity: str) -> None:
    helpers: dict[str, Callable[[], UUID]] = {
        "signal": lambda: make_signal_id("strategy.example", SYMBOL, CHECKPOINT, "observation"),
        "round": lambda: make_round_id(SIGNAL),
        "action": lambda: make_action_id(ROUND, "reevaluate", CHECKPOINT, ELIGIBLE),
        "intent": lambda: make_intent_id(ROUND, "proposal", ELIGIBLE, str(ACTION)),
    }
    expected = {"signal": SIGNAL, "round": ROUND, "action": ACTION, "intent": INTENT}[entity]
    assert [helpers[entity]() for _ in range(100)] == [expected] * 100


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": "strategy.other"},
        {"symbol": Symbol("OTHERUSDC")},
        {"checkpoint_at": CHECKPOINT + timedelta(microseconds=1)},
        {"kind": "other"},
        {"revision": 1},
    ],
)
def test_each_signal_component_changes_identity(changes: dict[str, object]) -> None:
    arguments = {
        "strategy_id": "strategy.example",
        "symbol": SYMBOL,
        "checkpoint_at": CHECKPOINT,
        "kind": "observation",
        **changes,
    }
    assert make_signal_id(**arguments) != SIGNAL  # type: ignore[arg-type]


@pytest.mark.parametrize("signal, revision", [(SignalId(UUID(int=1)), 0), (SIGNAL, 1)])
def test_each_round_component_changes_identity(signal: SignalId, revision: int) -> None:
    assert make_round_id(signal, revision) != ROUND


@pytest.mark.parametrize(
    "changes",
    [
        {"round_id": RoundId(UUID(int=1))},
        {"kind": "other"},
        {"created_at": CHECKPOINT + timedelta(microseconds=1)},
        {"not_before": ELIGIBLE + timedelta(microseconds=1)},
        {"revision": 1},
    ],
)
def test_each_action_component_changes_identity(changes: dict[str, object]) -> None:
    arguments = {
        "round_id": ROUND,
        "kind": "reevaluate",
        "created_at": CHECKPOINT,
        "not_before": ELIGIBLE,
        **changes,
    }
    assert make_action_id(**arguments) != ACTION  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"round_id": RoundId(UUID(int=1))},
        {"purpose": "other"},
        {"event_at": ELIGIBLE + timedelta(microseconds=1)},
        {"causal_ref": "different-fact"},
        {"revision": 1},
    ],
)
def test_each_intent_component_changes_identity(changes: dict[str, object]) -> None:
    arguments = {
        "round_id": ROUND,
        "purpose": "proposal",
        "event_at": ELIGIBLE,
        "causal_ref": str(ACTION),
        **changes,
    }
    assert make_intent_id(**arguments) != INTENT  # type: ignore[arg-type]


@pytest.mark.parametrize("entity", ["signal", "round", "action", "intent"])
@pytest.mark.parametrize("revision", [-1, True, False, 0.0, "0", None])
def test_revision_rejects_negative_boolean_and_non_integer_values(
    entity: str,
    revision: object,
) -> None:
    value = cast(int, revision)
    helpers: dict[str, Callable[[], UUID]] = {
        "signal": lambda: make_signal_id(
            "strategy.example", SYMBOL, CHECKPOINT, "observation", value
        ),
        "round": lambda: make_round_id(SIGNAL, value),
        "action": lambda: make_action_id(ROUND, "reevaluate", CHECKPOINT, ELIGIBLE, value),
        "intent": lambda: make_intent_id(ROUND, "proposal", ELIGIBLE, str(ACTION), value),
    }
    error = ValueError if type(revision) is int else TypeError
    with pytest.raises(error, match="revision"):
        helpers[entity]()


@pytest.mark.parametrize("field", ["checkpoint_at", "created_at", "not_before", "event_at"])
@pytest.mark.parametrize(
    "timestamp, error",
    [
        (datetime(2026, 10, 8, 14), ValueError),
        (CHECKPOINT.astimezone(timezone(timedelta(hours=-6))), ValueError),
        ("2026-10-08T14:00:00Z", TypeError),
        (None, TypeError),
    ],
)
def test_identity_timestamps_require_utc_datetime(
    field: str,
    timestamp: object,
    error: type[Exception],
) -> None:
    value = cast(datetime, timestamp)
    helpers: dict[str, Callable[[], UUID]] = {
        "checkpoint_at": lambda: make_signal_id("strategy.example", SYMBOL, value, "observation"),
        "created_at": lambda: make_action_id(ROUND, "reevaluate", value, ELIGIBLE),
        "not_before": lambda: make_action_id(ROUND, "reevaluate", CHECKPOINT, value),
        "event_at": lambda: make_intent_id(ROUND, "proposal", value, str(ACTION)),
    }
    with pytest.raises(error):
        helpers[field]()


@pytest.mark.parametrize(
    "field", ["strategy_id", "signal_kind", "action_kind", "purpose", "causal_ref"]
)
@pytest.mark.parametrize("value", ["", " \t\n", None, 1, 1.0, True])
def test_required_strings_are_not_coerced_or_blank(field: str, value: object) -> None:
    text = cast(str, value)
    helpers: dict[str, Callable[[], UUID]] = {
        "strategy_id": lambda: make_signal_id(text, SYMBOL, CHECKPOINT, "observation"),
        "signal_kind": lambda: make_signal_id("strategy.example", SYMBOL, CHECKPOINT, text),
        "action_kind": lambda: make_action_id(ROUND, text, CHECKPOINT, ELIGIBLE),
        "purpose": lambda: make_intent_id(ROUND, text, ELIGIBLE, str(ACTION)),
        "causal_ref": lambda: make_intent_id(ROUND, "proposal", ELIGIBLE, text),
    }
    error = ValueError if isinstance(value, str) else TypeError
    with pytest.raises(error):
        helpers[field]()


@pytest.mark.parametrize("helper", ["round", "action", "intent"])
@pytest.mark.parametrize("value", ["b646573c-50f1-51fa-b826-244806b65abc", None, 1, True])
def test_parent_ids_require_real_uuid(helper: str, value: object) -> None:
    helpers: dict[str, Callable[[], UUID]] = {
        "round": lambda: make_round_id(cast(SignalId, value)),
        "action": lambda: make_action_id(cast(RoundId, value), "reevaluate", CHECKPOINT, ELIGIBLE),
        "intent": lambda: make_intent_id(cast(RoundId, value), "proposal", ELIGIBLE, str(ACTION)),
    }
    with pytest.raises(TypeError):
        helpers[helper]()


@pytest.mark.parametrize("symbol", ["TESTUSDC", None, 1, True])
def test_signal_requires_symbol_value_object(symbol: object) -> None:
    with pytest.raises(TypeError):
        make_signal_id("strategy.example", cast(Symbol, symbol), CHECKPOINT, "observation")


def test_action_accepts_immediate_eligibility_and_rejects_backwards_time() -> None:
    assert make_action_id(ROUND, "reevaluate", CHECKPOINT, CHECKPOINT).version == 5
    with pytest.raises(ValueError, match="not_before"):
        make_action_id(ROUND, "reevaluate", CHECKPOINT, CHECKPOINT - timedelta(microseconds=1))


def test_normalized_symbol_and_canonical_uuid_have_one_identity() -> None:
    assert (
        make_signal_id("strategy.example", Symbol("TESTUSDC"), CHECKPOINT, "observation") == SIGNAL
    )
    assert make_round_id(SignalId(UUID(str(SIGNAL).upper()))) == ROUND
    assert (
        make_action_id(RoundId(UUID(str(ROUND).upper())), "reevaluate", CHECKPOINT, ELIGIBLE)
        == ACTION
    )


@pytest.mark.parametrize("left, right", [(("ab", "c"), ("a", "bc")), (("a|b", "c"), ("a", "b|c"))])
def test_structurally_different_components_do_not_collide(
    left: tuple[str, str],
    right: tuple[str, str],
) -> None:
    assert make_intent_id(ROUND, left[0], ELIGIBLE, left[1]) != make_intent_id(
        ROUND, right[0], ELIGIBLE, right[1]
    )
    assert make_signal_id(left[0], SYMBOL, CHECKPOINT, left[1]) != make_signal_id(
        right[0], SYMBOL, CHECKPOINT, right[1]
    )


def test_strings_remain_exact_without_hidden_trimming_or_unicode_normalization() -> None:
    assert make_signal_id("strategy.example ", SYMBOL, CHECKPOINT, "observation") != SIGNAL
    assert make_intent_id(ROUND, 'proposal"\\\n', ELIGIBLE, 'fact|["x"]').version == 5
    assert make_intent_id(ROUND, "é", ELIGIBLE, "fact") != make_intent_id(
        ROUND, "e\u0301", ELIGIBLE, "fact"
    )


def test_identity_helpers_do_not_construct_models_or_make_ids_optional() -> None:
    for model, field in (
        (Signal, "signal_id"),
        (Round, "round_id"),
        (ScheduledAction, "action_id"),
        (OrderIntent, "intent_id"),
    ):
        identifier = next(item for item in fields(model) if item.name == field)
        assert identifier.default is MISSING
        assert identifier.default_factory is MISSING


def test_unicode_and_json_escaping_golden_vector() -> None:
    assert make_intent_id(ROUND, "observación", ELIGIBLE, 'fact|"\\\n', revision=2) == UUID(
        "7a40d413-4bc6-5eeb-93c7-d4ea980c2c80"
    )


def test_only_strategy_owned_ids_have_public_derivation_helpers() -> None:
    assert {name for name in vars(identity_module) if name.startswith("make_")} == {
        "make_signal_id",
        "make_round_id",
        "make_action_id",
        "make_intent_id",
    }
