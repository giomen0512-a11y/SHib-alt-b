"""Simulación offline opt-in; no implementa un adaptador LIVE."""

from trading_engine.replay.simulated_execution.core import SimulatedExecution
from trading_engine.replay.simulated_execution.policy import SimulationPolicy

__all__ = ["SimulatedExecution", "SimulationPolicy"]
