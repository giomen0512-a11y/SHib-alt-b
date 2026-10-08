"""Resultados de estrategia y riesgo; sin reglas financieras."""

from dataclasses import dataclass

from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.orders import OrderIntent
from trading_engine.models.signals import Signal


@dataclass(frozen=True)
class StrategyResult:
    signals: tuple[Signal, ...] = ()
    scheduled_actions: tuple[ScheduledAction, ...] = ()
    order_intents: tuple[OrderIntent, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.signals, tuple, "signals")
        require_type(self.scheduled_actions, tuple, "scheduled_actions")
        require_type(self.order_intents, tuple, "order_intents")
        for signal in self.signals:
            require_type(signal, Signal, "signal")
        for action in self.scheduled_actions:
            require_type(action, ScheduledAction, "scheduled_action")
        for intent in self.order_intents:
            require_type(intent, OrderIntent, "order_intent")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        require_type(self.approved, bool, "approved")
        if self.reason is not None:
            nonblank(self.reason, "reason")
        if not self.approved and self.reason is None:
            raise ValueError("Un rechazo requiere reason")
