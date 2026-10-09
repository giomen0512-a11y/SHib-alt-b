"""Estado y resultados inmutables del scheduling genérico."""

from dataclasses import dataclass

from trading_engine.models._validation import require_type
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyState
from trading_engine.models.orders import OrderCancelRequest, OrderIntent, validate_cancel_requests
from trading_engine.models.signals import Signal


def _canonical_actions(actions: tuple[ScheduledAction, ...]) -> tuple[ScheduledAction, ...]:
    require_type(actions, tuple, "actions")
    for action in actions:
        require_type(action, ScheduledAction, "action")
    if len({action.action_id for action in actions}) != len(actions):
        raise ValueError("action_id duplicado")
    return tuple(sorted(actions, key=lambda action: (action.not_before, action.action_id.int)))


@dataclass(frozen=True)
class CoordinatorState:
    pending_actions: tuple[ScheduledAction, ...] = ()
    strategy_state: StrategyState | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pending_actions", _canonical_actions(self.pending_actions))
        if self.strategy_state is not None:
            require_type(self.strategy_state, StrategyState, "strategy_state")
            self.strategy_state.validate_pending_actions(self.pending_actions)


@dataclass(frozen=True)
class CoordinationResult:
    signals: tuple[Signal, ...]
    order_intents: tuple[OrderIntent, ...]
    state: CoordinatorState
    cancel_requests: tuple[OrderCancelRequest, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.signals, tuple, "signals")
        for signal in self.signals:
            require_type(signal, Signal, "signal")
        require_type(self.order_intents, tuple, "order_intents")
        for intent in self.order_intents:
            require_type(intent, OrderIntent, "order_intent")
        require_type(self.state, CoordinatorState, "state")
        validate_cancel_requests(self.cancel_requests)
        if {i.intent_id for i in self.order_intents} & {r.intent_id for r in self.cancel_requests}:
            raise ValueError("No se puede emitir y cancelar la misma intención")


@dataclass(frozen=True)
class DueActionsResult:
    due_actions: tuple[ScheduledAction, ...]
    state: CoordinatorState

    def __post_init__(self) -> None:
        require_type(self.state, CoordinatorState, "state")
        object.__setattr__(self, "due_actions", _canonical_actions(self.due_actions))
