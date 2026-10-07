"""Contrato de datos; la coordinación decide el origen y la disponibilidad."""

from collections.abc import Iterator
from typing import Protocol

from trading_engine.models.market import Candle
from trading_engine.models.values import Symbol


class MarketDataProvider(Protocol):
    def candles(self, symbol: Symbol) -> Iterator[Candle]: ...
