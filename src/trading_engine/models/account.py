"""Snapshots sin cálculo de PnL, margen o liquidación."""

from dataclasses import dataclass
from datetime import datetime

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.enums import PositionSide
from trading_engine.models.values import Money, Price, Quantity, Symbol


@dataclass(frozen=True)
class Position:
    symbol: Symbol
    side: PositionSide
    quantity: Quantity
    average_entry_price: Price
    timestamp: datetime

    def __post_init__(self) -> None:
        require_type(self.symbol, Symbol, "symbol")
        require_type(self.side, PositionSide, "side")
        require_type(self.quantity, Quantity, "quantity")
        require_type(self.average_entry_price, Price, "average_entry_price")
        utc_timestamp(self.timestamp, "timestamp")


@dataclass(frozen=True)
class BalanceSnapshot:
    wallet_balance: Money
    available_balance: Money
    timestamp: datetime

    def __post_init__(self) -> None:
        require_type(self.wallet_balance, Money, "wallet_balance")
        require_type(self.available_balance, Money, "available_balance")
        utc_timestamp(self.timestamp, "timestamp")
        if self.wallet_balance.currency != self.available_balance.currency:
            raise ValueError("Los balances deben usar la misma moneda")
        if self.wallet_balance.amount < 0 or self.available_balance.amount < 0:
            raise ValueError("Los balances no pueden ser negativos")
