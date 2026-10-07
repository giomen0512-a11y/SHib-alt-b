"""Identidad y snapshot temporal de ronda; sin máquina de transiciones."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.enums import RoundStatus
from trading_engine.models.ids import RoundId
from trading_engine.models.values import Symbol


@dataclass(frozen=True)
class Round:
    round_id: RoundId
    symbol: Symbol
    status: RoundStatus
    created_at: datetime
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        require_type(self.round_id, UUID, "round_id")
        require_type(self.symbol, Symbol, "symbol")
        require_type(self.status, RoundStatus, "status")
        utc_timestamp(self.created_at, "created_at")
        for name in ("opened_at", "closed_at"):
            value = getattr(self, name)
            if value is not None:
                utc_timestamp(value, name)
                if value < self.created_at:
                    raise ValueError(f"{name} no puede ser anterior a created_at")
        if self.opened_at is not None and self.closed_at is not None:
            if self.closed_at < self.opened_at:
                raise ValueError("closed_at no puede ser anterior a opened_at")
        if self.status is RoundStatus.PENDING_ENTRY:
            if self.opened_at is not None or self.closed_at is not None:
                raise ValueError("PENDING_ENTRY no admite apertura ni cierre")
        elif self.status in (RoundStatus.OPEN, RoundStatus.CLOSING):
            if self.opened_at is None or self.closed_at is not None:
                raise ValueError("OPEN/CLOSING requiere opened_at y no admite closed_at")
        elif self.status is RoundStatus.CLOSED:
            if self.opened_at is None or self.closed_at is None:
                raise ValueError("CLOSED requiere opened_at y closed_at")
