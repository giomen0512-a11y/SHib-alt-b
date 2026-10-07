"""Señal genérica, sin parámetros ni condiciones de una estrategia."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from trading_engine.models._validation import nonblank, require_type, utc_timestamp
from trading_engine.models.ids import SignalId
from trading_engine.models.values import Symbol


@dataclass(frozen=True)
class Signal:
    signal_id: SignalId
    symbol: Symbol
    timestamp: datetime
    kind: str

    def __post_init__(self) -> None:
        require_type(self.signal_id, UUID, "signal_id")
        require_type(self.symbol, Symbol, "symbol")
        utc_timestamp(self.timestamp, "timestamp")
        nonblank(self.kind, "kind")
