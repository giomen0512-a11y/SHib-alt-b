"""Snapshot explícito de entrada y Base; sin balances ni persistencia."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyState
from trading_engine.models.identity import make_action_id, make_round_id, make_signal_id
from trading_engine.models.orders import OrderCancelRequest
from trading_engine.models.signals import Signal
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.base import BaseState
from trading_engine.strategy.alt_b.events.round_state import AltBRoundState, RoundRecord
from trading_engine.strategy.alt_b.state_market import AltBMarketState

SIGNAL_KIND: Final[str] = "ALT_B_LONG_ENTRY"
ACTION_KIND: Final[str] = "ALT_B_BASE_ENTRY"
ENTRY_DELAY: Final[timedelta] = timedelta(seconds=120)


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
    base: BaseState | None = None
    round_state: AltBRoundState | None = None
    round_history: tuple[RoundRecord, ...] = ()
    base_cancel_request: OrderCancelRequest | None = None

    def validate_pending_actions(self, actions: tuple[ScheduledAction, ...]) -> None:
        # Cola vacía también es válida tras advance_time: la reserva estratégica permanece.
        if any(
            self.pending_entry is None or action != self.pending_entry.action for action in actions
        ):
            raise ValueError("Cola de scheduling incompatible con el estado de entrada")

    def __post_init__(self) -> None:
        require_type(self.market, AltBMarketState, "market")
        require_type(self.allow_entries, bool, "allow_entries")
        require_type(self.round_history, tuple, "round_history")
        for record in self.round_history:
            require_type(record, RoundRecord, "round_record")
        if self.round_state is not None:
            require_type(self.round_state, AltBRoundState, "round_state")
            if self.base is None:
                raise ValueError("Ronda sin Base")
            self.round_state.validate_base(self.base)
        if self.base_cancel_request is not None:
            require_type(self.base_cancel_request, OrderCancelRequest, "base_cancel_request")
            if self.base is None or self.base_cancel_request.intent != self.base.intent:
                raise ValueError("Cancelación Base ajena")
            if (
                self.last_event_at is None
                or self.base_cancel_request.created_at > self.last_event_at
            ):
                raise ValueError("Cancelación Base posterior al cursor causal")
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
        if self.base is not None:
            require_type(self.base, BaseState, "base")
            if self.pending_entry is not None:
                raise ValueError("Base consumida no admite otra entrada pendiente")
            signal, expected = entry_identity(
                AltBConfig(symbol=self.base.action.symbol), self.base.action.created_at
            )
            PendingEntry(signal, self.base.action)  # También exige checkpoint exacto 15m.
            if self.base.action != expected:
                raise ValueError("Identidad de acción de Base no verificable")
            observed_times = (
                self.base.intent.created_at,
                *(f.timestamp for f in self.base.fills),
                *((self.base.order.updated_at,) if self.base.order is not None else ()),
            )
            if self.last_event_at is None or any(t > self.last_event_at for t in observed_times):
                raise ValueError("Base posterior al cursor causal")
        all_fills = list(self.base.fills if self.base else ())
        if self.round_state is not None:
            all_fills += [f for leg in self.round_state.legs for f in leg.fills]
        for record in self.round_history:
            all_fills += list(record.round_state.entry_fills(record.base))
            all_fills += list(record.round_state.exit_fills)
        if len({f.fill_id for f in all_fills}) != len(all_fills):
            raise ValueError("FillId duplicado entre rondas o niveles")
        if any(self.last_event_at is None or f.timestamp > self.last_event_at for f in all_fills):
            raise ValueError("Fill posterior al cursor causal")
        rounds = [r.base.intent.round_id for r in self.round_history]
        if self.base is not None:
            rounds.append(self.base.intent.round_id)
        if len(set(rounds)) != len(rounds):
            raise ValueError("RoundId duplicado")
        orders = [self.base.order] if self.base is not None and self.base.order is not None else []
        orders += [
            record.base.order for record in self.round_history if record.base.order is not None
        ]
        for management in (
            *((self.round_state,) if self.round_state is not None else ()),
            *(record.round_state for record in self.round_history),
        ):
            dates = (
                management.opened_at,
                management.closed_at,
                *(r.created_at for r in management.cancel_requests),
            )
            if any(
                t is not None and (self.last_event_at is None or t > self.last_event_at)
                for t in dates
            ):
                raise ValueError("Ronda posterior al cursor causal")
            for leg in management.legs:
                times = (leg.intent.created_at, *((leg.order.updated_at,) if leg.order else ()))
                if any(self.last_event_at is None or t > self.last_event_at for t in times):
                    raise ValueError("Orden posterior al cursor causal")
                if leg.order is not None:
                    orders.append(leg.order)
        if len({o.order_id for o in orders}) != len(orders):
            raise ValueError("OrderId duplicado entre niveles o rondas")
        if any(self.last_event_at is None or o.updated_at > self.last_event_at for o in orders):
            raise ValueError("Orden posterior al cursor causal")
