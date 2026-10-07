"""Estado y resultados inmutables del scheduling genérico."""

from dataclasses import dataclass

from trading_engine.models._validation import require_type
from trading_engine.models.orders import OrderIntent
from trading_engine.models.signals import Signal


def _canonical_intents(intents: tuple[OrderIntent, ...]) -> tuple[OrderIntent, ...]:
    require_type(intents, tuple, "intents")
    for intent in intents:
        require_type(intent, OrderIntent, "intent")
    if len({intent.intent_id for intent in intents}) != len(intents):
        raise ValueError("intent_id duplicado")
    return tuple(sorted(intents, key=lambda intent: (intent.not_before, intent.intent_id.int)))


@dataclass(frozen=True)
class CoordinatorState:
    pending_intents: tuple[OrderIntent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pending_intents", _canonical_intents(self.pending_intents))


@dataclass(frozen=True)
class CoordinationResult:
    signals: tuple[Signal, ...]
    state: CoordinatorState

    def __post_init__(self) -> None:
        require_type(self.signals, tuple, "signals")
        for signal in self.signals:
            require_type(signal, Signal, "signal")
        require_type(self.state, CoordinatorState, "state")


@dataclass(frozen=True)
class DueIntentsResult:
    due_intents: tuple[OrderIntent, ...]
    state: CoordinatorState

    def __post_init__(self) -> None:
        require_type(self.state, CoordinatorState, "state")
        object.__setattr__(self, "due_intents", _canonical_intents(self.due_intents))
