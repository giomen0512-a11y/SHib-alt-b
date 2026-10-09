"""Único contrato de estrategia compartido por los futuros modos."""

from datetime import datetime
from typing import Protocol

from trading_engine.models.account import Position
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyResult, StrategyState
from trading_engine.models.ids import ActionId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order
from trading_engine.models.rounds import Round


class StrategyEngine(Protocol):
    def on_candle(
        self,
        candle: Candle,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult: ...

    def on_quote(
        self,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult: ...

    def on_order(
        self,
        order: Order,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult: ...

    def on_fill(
        self,
        fill: Fill,
        order: Order,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult: ...

    def on_scheduled_action(
        self,
        action: ScheduledAction,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult: ...

    def on_entry_control(
        self,
        allow_entries: bool,
        at: datetime,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult: ...

    def on_action_invalidated(
        self,
        action_id: ActionId,
        at: datetime,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult: ...
