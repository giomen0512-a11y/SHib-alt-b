"""Acción futura genérica, sin cantidad ni parámetros de una orden."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from trading_engine.models._validation import nonblank, require_type, utc_timestamp
from trading_engine.models.ids import ActionId, RoundId
from trading_engine.models.values import Symbol


@dataclass(frozen=True)
class ScheduledAction:
    action_id: ActionId
    round_id: RoundId
    symbol: Symbol
    kind: str
    created_at: datetime
    not_before: datetime

    def __post_init__(self) -> None:
        require_type(self.action_id, UUID, "action_id")
        require_type(self.round_id, UUID, "round_id")
        require_type(self.symbol, Symbol, "symbol")
        nonblank(self.kind, "kind")
        utc_timestamp(self.created_at, "created_at")
        utc_timestamp(self.not_before, "not_before")
        if self.not_before < self.created_at:
            raise ValueError("not_before no puede ser anterior a created_at")
