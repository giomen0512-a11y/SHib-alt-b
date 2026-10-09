"""Estado lógico explícito de señales; no balances, órdenes ni persistencia."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyState
from trading_engine.models.identity import make_action_id, make_round_id, make_signal_id
from trading_engine.models.signals import Signal
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.state_market import AltBMarketState

SIGNAL_KIND: Final[str] = "ALT_B_LONG_ENTRY"
ACTION_KIND: Final[str] = "ALT_B_BASE_ENTRY"
ENTRY_DELAY: Final[timedelta] = timedelta(seconds=120)
BASE_EXECUTION_PENDING: Final[str] = "base_execution_not_implemented_3b_2c"


def entry_identity(config: AltBConfig, at: datetime) -> tuple[Signal, ScheduledAction]:
    """Revision cero: el checkpoint causal identifica el mismo hecho en todo modo."""
    signal = Signal(
        make_signal_id(config.strategy_id, config.symbol, at, SIGNAL_KIND),
        config.symbol,
        at,
        SIGNAL_KIND,
    )
    round_id = make_round_id(signal.signal_id)
    action = ScheduledAction(
        make_action_id(round_id, ACTION_KIND, at, at + ENTRY_DELAY),
        round_id,
        config.symbol,
        ACTION_KIND,
        at,
        at + ENTRY_DELAY,
    )
    return signal, action


@dataclass(frozen=True)
class PendingEntry:
    signal: Signal
    action: ScheduledAction
    evaluated_at: datetime | None = None

    def __post_init__(self) -> None:
        require_type(self.signal, Signal, "signal")
        require_type(self.action, ScheduledAction, "action")
        expected = entry_identity(AltBConfig(symbol=self.signal.symbol), self.signal.timestamp)
        if (self.signal, self.action) != expected:
            raise ValueError("Identidad de entrada no verificable")
        if self.signal.timestamp.minute % 15 or self.signal.timestamp.second:
            raise ValueError("La señal requiere checkpoint 15m")
        if self.signal.timestamp.microsecond:
            raise ValueError("La señal requiere checkpoint exacto")
        if self.evaluated_at is not None:
            utc_timestamp(self.evaluated_at, "evaluated_at")
            if self.evaluated_at < self.action.not_before:
                raise ValueError("Evaluación anterior a +120s")


@dataclass(frozen=True)
class AltBStrategyState(StrategyState):
    market: AltBMarketState = AltBMarketState()
    allow_entries: bool = False
    pending_entry: PendingEntry | None = None
    last_event_at: datetime | None = None
    block_reason: str | None = None

    def validate_pending_actions(self, actions: tuple[ScheduledAction, ...]) -> None:
        # Cola vacía también es válida tras advance_time: la reserva estratégica permanece.
        if any(
            self.pending_entry is None or action != self.pending_entry.action for action in actions
        ):
            raise ValueError("Cola de scheduling incompatible con el estado de entrada")

    def __post_init__(self) -> None:
        require_type(self.market, AltBMarketState, "market")
        require_type(self.allow_entries, bool, "allow_entries")
        if self.last_event_at is not None:
            utc_timestamp(self.last_event_at, "last_event_at")
        if self.market.candles_1m:
            if (
                self.last_event_at is None
                or self.market.candles_1m[-1].close_time > self.last_event_at
            ):
                raise ValueError("Historial posterior al cursor causal")
        if self.block_reason is not None:
            require_type(self.block_reason, str, "block_reason")
            if not self.block_reason.strip():
                raise ValueError("block_reason vacío")
        if self.pending_entry is not None:
            require_type(self.pending_entry, PendingEntry, "pending_entry")
            if not self.allow_entries or not self.market.candles_1m:
                raise ValueError("Entrada pendiente sin permiso/historial verificable")
            signal = self.pending_entry.signal
            if signal.symbol != self.market.candles_1m[-1].symbol:
                raise ValueError("Entrada e historial deben tener el mismo símbolo")
            if signal.timestamp > self.market.candles_1m[-1].close_time:
                raise ValueError("Entrada posterior al historial causal")
            evaluated = self.pending_entry.evaluated_at
            if evaluated is not None and (
                self.last_event_at is None or evaluated > self.last_event_at
            ):
                raise ValueError("Evaluación posterior al cursor causal")
