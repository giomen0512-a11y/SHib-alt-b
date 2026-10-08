"""Velas y quotes independientes del proveedor de mercado."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_engine.models._validation import finite_decimal, require_type, utc_timestamp
from trading_engine.models.values import Price, Symbol


@dataclass(frozen=True)
class Candle:
    symbol: Symbol
    open_time: datetime
    close_time: datetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Decimal
    taker_buy_base_volume: Decimal | None = None

    def __post_init__(self) -> None:
        require_type(self.symbol, Symbol, "symbol")
        utc_timestamp(self.open_time, "open_time")
        utc_timestamp(self.close_time, "close_time")
        if self.open_time >= self.close_time:
            raise ValueError("open_time debe ser anterior a close_time")
        for name in ("open", "high", "low", "close"):
            require_type(getattr(self, name), Price, name)
        if self.high.value < max(self.open.value, self.close.value, self.low.value):
            raise ValueError("high no puede ser inferior a open, close o low")
        if self.low.value > min(self.open.value, self.close.value, self.high.value):
            raise ValueError("low no puede ser superior a open, close o high")
        finite_decimal(self.volume, "volume", nonnegative=True)
        if self.taker_buy_base_volume is not None:
            finite_decimal(self.taker_buy_base_volume, "taker_buy_base_volume", nonnegative=True)
            if self.taker_buy_base_volume > self.volume:
                raise ValueError("taker_buy_base_volume no puede superar volume")


@dataclass(frozen=True)
class Quote:
    symbol: Symbol
    timestamp: datetime
    bid: Price
    ask: Price

    def __post_init__(self) -> None:
        require_type(self.symbol, Symbol, "symbol")
        utc_timestamp(self.timestamp, "timestamp")
        require_type(self.bid, Price, "bid")
        require_type(self.ask, Price, "ask")
        if self.bid.value > self.ask.value:
            raise ValueError("bid no puede superar ask")
