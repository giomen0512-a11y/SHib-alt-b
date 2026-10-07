"""Límite de evaluación de intenciones, sin reglas de riesgo implementadas."""

from typing import Protocol

from trading_engine.models.account import BalanceSnapshot, Position
from trading_engine.models.decisions import RiskDecision
from trading_engine.models.orders import OrderIntent


class RiskManager(Protocol):
    def evaluate(
        self, intent: OrderIntent, balance: BalanceSnapshot, position: Position | None
    ) -> RiskDecision: ...
