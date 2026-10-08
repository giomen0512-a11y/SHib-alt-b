"""Transformación de estado con tiempo explícito, sin efectos externos."""

from datetime import datetime

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.account import Position
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyResult
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order
from trading_engine.models.rounds import Round
from trading_engine.orchestration.models import (
    CoordinationResult,
    CoordinatorState,
    DueActionsResult,
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
        self._validate_context(state, position, round_state)
        require_type(candle, Candle, "candle")
        return self._integrate(
            state, self._strategy.on_candle(candle, position, round_state), candle.close_time
        )

    def on_quote(
        self,
        state: CoordinatorState,
        quote: Quote,
        position: Position | None = None,
        round_state: Round | None = None,
    ) -> CoordinationResult:
        self._validate_context(state, position, round_state)
        require_type(quote, Quote, "quote")
        return self._integrate(
            state, self._strategy.on_quote(quote, position, round_state), quote.timestamp
        )

    def on_order(
        self,
        state: CoordinatorState,
        order: Order,
        position: Position | None = None,
        round_state: Round | None = None,
    ) -> CoordinationResult:
        self._validate_context(state, position, round_state)
        require_type(order, Order, "order")
        return self._integrate(
            state, self._strategy.on_order(order, position, round_state), order.updated_at
        )

    def on_fill(
        self,
        state: CoordinatorState,
        fill: Fill,
        order: Order,
        position: Position | None = None,
        round_state: Round | None = None,
    ) -> CoordinationResult:
        self._validate_context(state, position, round_state)
        require_type(fill, Fill, "fill")
        require_type(order, Order, "order")
        if fill.order_id != order.order_id:
            raise ValueError("fill.order_id debe coincidir con order.order_id")
        return self._integrate(
            state, self._strategy.on_fill(fill, order, position, round_state), fill.timestamp
        )

    def on_scheduled_action(
        self,
        state: CoordinatorState,
        action: ScheduledAction,
        quote: Quote,
        position: Position | None = None,
        round_state: Round | None = None,
    ) -> CoordinationResult:
        self._validate_context(state, position, round_state)
        require_type(action, ScheduledAction, "action")
        require_type(quote, Quote, "quote")
        if action.symbol != quote.symbol:
            raise ValueError("quote.symbol debe coincidir con action.symbol")
        if quote.timestamp < action.not_before:
            raise ValueError("quote.timestamp no puede ser anterior a action.not_before")
        return self._integrate(
            state,
            self._strategy.on_scheduled_action(action, quote, position, round_state),
            quote.timestamp,
        )

    @staticmethod
    def _validate_context(
        state: CoordinatorState, position: Position | None, round_state: Round | None
    ) -> None:
        require_type(state, CoordinatorState, "state")
        if position is not None:
            require_type(position, Position, "position")
        if round_state is not None:
            require_type(round_state, Round, "round_state")

    @staticmethod
    def _integrate(
        state: CoordinatorState, result: StrategyResult, event_time: datetime
    ) -> CoordinationResult:
        require_type(result, StrategyResult, "strategy_result")
        for action in result.scheduled_actions:
            if action.created_at < event_time:
                raise ValueError("ScheduledAction.created_at no puede ser anterior a event_time")
        for intent in result.order_intents:
            if intent.created_at < event_time:
                raise ValueError("OrderIntent.created_at no puede ser anterior a event_time")
        updated = CoordinatorState(state.pending_actions + result.scheduled_actions)
        return CoordinationResult(
            signals=result.signals, order_intents=result.order_intents, state=updated
        )

    def advance_time(self, state: CoordinatorState, now: datetime) -> DueActionsResult:
        require_type(state, CoordinatorState, "state")
        utc_timestamp(now, "now")
        due: list[ScheduledAction] = []
        pending: list[ScheduledAction] = []
        for action in state.pending_actions:
            if action.not_before <= now:
                due.append(action)
            else:
                pending.append(action)
        return DueActionsResult(due_actions=tuple(due), state=CoordinatorState(tuple(pending)))
