"""Resultados de estrategia y riesgo; sin reglas financieras."""

from dataclasses import dataclass
from uuid import UUID

from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.ids import ActionId
from trading_engine.models.orders import OrderIntent
from trading_engine.models.signals import Signal


@dataclass(frozen=True)
class StrategyState:
    """Base de snapshots explícitos; el dominio genérico no conoce su contenido."""

    def validate_pending_actions(self, actions: tuple[ScheduledAction, ...]) -> None:
        """La estrategia puede validar su cola; el snapshot genérico no impone reglas."""


@dataclass(frozen=True)
class StrategyResult:
    signals: tuple[Signal, ...] = ()
    scheduled_actions: tuple[ScheduledAction, ...] = ()
    order_intents: tuple[OrderIntent, ...] = ()
    strategy_state: StrategyState | None = None
    cancelled_action_ids: tuple[ActionId, ...] = ()

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
        if self.strategy_state is not None:
            require_type(self.strategy_state, StrategyState, "strategy_state")
        require_type(self.cancelled_action_ids, tuple, "cancelled_action_ids")
        for action_id in self.cancelled_action_ids:
            require_type(action_id, UUID, "cancelled_action_id")
        if len(set(self.cancelled_action_ids)) != len(self.cancelled_action_ids):
            raise ValueError("cancelled_action_id duplicado")
        if any(a.action_id in self.cancelled_action_ids for a in self.scheduled_actions):
            raise ValueError("No se puede programar y cancelar la misma acción")


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
