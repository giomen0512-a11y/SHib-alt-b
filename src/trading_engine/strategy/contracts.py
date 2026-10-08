"""Único contrato de estrategia compartido por los futuros modos."""

from typing import Protocol

from trading_engine.models.account import Position
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyResult
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order
from trading_engine.models.rounds import Round


class StrategyEngine(Protocol):
    def on_candle(
        self, candle: Candle, position: Position | None, round_state: Round | None
    ) -> StrategyResult: ...

    def on_quote(
        self, quote: Quote, position: Position | None, round_state: Round | None
    ) -> StrategyResult: ...

    def on_order(
        self, order: Order, position: Position | None, round_state: Round | None
    ) -> StrategyResult: ...

    def on_fill(
        self, fill: Fill, order: Order, position: Position | None, round_state: Round | None
    ) -> StrategyResult: ...

    def on_scheduled_action(
        self,
        action: ScheduledAction,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
    ) -> StrategyResult: ...
