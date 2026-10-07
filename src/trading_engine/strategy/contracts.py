"""Único contrato de estrategia compartido por los futuros modos."""

from typing import Protocol

from trading_engine.models.account import Position
from trading_engine.models.decisions import StrategyResult
from trading_engine.models.market import Candle
from trading_engine.models.rounds import Round


class StrategyEngine(Protocol):
    def on_candle(
        self, candle: Candle, position: Position | None, round_state: Round | None
    ) -> StrategyResult: ...
