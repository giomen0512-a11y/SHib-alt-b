"""Identidad UUIDv5 pura a partir de componentes explícitos del dominio."""

from datetime import datetime
from json import dumps
from typing import Final
from uuid import UUID, uuid5

from trading_engine.models._validation import nonblank, require_type, utc_timestamp
from trading_engine.models.ids import ActionId, IntentId, RoundId, SignalId
from trading_engine.models.values import Symbol

PROJECT_NAMESPACE: Final[UUID] = UUID("22842300-09df-530e-93ea-1e985518f6d1")
IDENTITY_SCHEMA_VERSION: Final[str] = "1"


def _timestamp(value: datetime, name: str) -> str:
    utc_timestamp(value, name)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _revision(value: int) -> str:
    require_type(value, int, "revision")
    if isinstance(value, bool):
        raise TypeError("revision debe ser int, no bool")
    if value < 0:
        raise ValueError("revision no puede ser negativa")
    return str(value)


def _derive(entity: str, *components: str) -> UUID:
    material = dumps(
        [f"id-v{IDENTITY_SCHEMA_VERSION}", entity, *components],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid5(PROJECT_NAMESPACE, material)


def make_signal_id(
    strategy_id: str,
    symbol: Symbol,
    checkpoint_at: datetime,
    kind: str,
    revision: int = 0,
) -> SignalId:
    nonblank(strategy_id, "strategy_id")
    require_type(symbol, Symbol, "symbol")
    nonblank(kind, "kind")
    return SignalId(
        _derive(
            "signal",
            strategy_id,
            symbol.value,
            _timestamp(checkpoint_at, "checkpoint_at"),
            kind,
            _revision(revision),
        )
    )


def make_round_id(signal_id: SignalId, revision: int = 0) -> RoundId:
    require_type(signal_id, UUID, "signal_id")
    return RoundId(_derive("round", str(signal_id), _revision(revision)))


def make_action_id(
    round_id: RoundId,
    kind: str,
    created_at: datetime,
    not_before: datetime,
    revision: int = 0,
) -> ActionId:
    require_type(round_id, UUID, "round_id")
    nonblank(kind, "kind")
    created = _timestamp(created_at, "created_at")
    eligible = _timestamp(not_before, "not_before")
    if not_before < created_at:
        raise ValueError("not_before no puede ser anterior a created_at")
    return ActionId(_derive("action", str(round_id), kind, created, eligible, _revision(revision)))


def make_intent_id(
    round_id: RoundId,
    purpose: str,
    event_at: datetime,
    causal_ref: str,
    revision: int = 0,
) -> IntentId:
    require_type(round_id, UUID, "round_id")
    nonblank(purpose, "purpose")
    nonblank(causal_ref, "causal_ref")
    return IntentId(
        _derive(
            "intent",
            str(round_id),
            purpose,
            _timestamp(event_at, "event_at"),
            causal_ref,
            _revision(revision),
        )
    )
