"""Riesgo aislado offline opt-in; sin liquidación, transferencias o transporte."""

from trading_engine.replay.isolated_margin_risk.core import IsolatedMarginRisk
from trading_engine.replay.isolated_margin_risk.policy import MarginAllocationPolicy

__all__ = ["IsolatedMarginRisk", "MarginAllocationPolicy"]
