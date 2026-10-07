"""Transformación de estado con tiempo explícito, sin efectos externos."""

from datetime import datetime

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.account import Position
from trading_engine.models.decisions import StrategyResult
from trading_engine.models.market import Candle
from trading_engine.models.orders import OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.orchestration.models import (
    CoordinationResult,
    CoordinatorState,
    DueIntentsResult,
)
from trading_engine.strategy.contracts import StrategyEngine


class StrategyCoordinator:
    def __init__(self, strategy: StrategyEngine) -> None:
        self._strategy = strategy

    def on_candle(
        self,
        state: CoordinatorState,
        candle: Candle,
        position: Position | None = None,
        round_state: Round | None = None,
    ) -> CoordinationResult:
        require_type(state, CoordinatorState, "state")
        require_type(candle, Candle, "candle")
        if position is not None:
            require_type(position, Position, "position")
        if round_state is not None:
            require_type(round_state, Round, "round_state")
        result = self._strategy.on_candle(candle, position, round_state)
        require_type(result, StrategyResult, "strategy_result")
        updated = CoordinatorState(state.pending_intents + result.order_intents)
        return CoordinationResult(signals=result.signals, state=updated)

    def advance_time(self, state: CoordinatorState, now: datetime) -> DueIntentsResult:
        require_type(state, CoordinatorState, "state")
        utc_timestamp(now, "now")
        due: list[OrderIntent] = []
        pending: list[OrderIntent] = []
        for intent in state.pending_intents:
            if intent.not_before <= now:
                due.append(intent)
            else:
                pending.append(intent)
        return DueIntentsResult(due_intents=tuple(due), state=CoordinatorState(tuple(pending)))
