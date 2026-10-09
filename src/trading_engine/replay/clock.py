"""Reloj lógico inmutable, sin espera ni reloj del sistema."""

from dataclasses import dataclass
from datetime import datetime

from trading_engine.utils.time import require_utc


@dataclass(frozen=True)
class SimulatedClock:
    now: datetime

    def __post_init__(self) -> None:
        require_utc(self.now)

    def advance(self, at: datetime) -> "SimulatedClock":
        require_utc(at)
        if at < self.now:
            raise ValueError("El reloj lógico no puede retroceder")
        return SimulatedClock(at)
