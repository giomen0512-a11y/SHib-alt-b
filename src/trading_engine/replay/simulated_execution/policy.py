"""Supuestos explícitos de simulación offline, sin valores de mercado por defecto."""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from hashlib import sha256

from trading_engine.models._validation import nonblank, require_type
from trading_engine.replay.encoding import encoded
from trading_engine.replay.execution_model.policy import ExecutionPolicy


class QueuePolicy(StrEnum):
    WITHHOLD = "WITHHOLD_WITHOUT_QUEUE_EVIDENCE"
    BUDGET_FIFO = "ASSUME_SHARED_VOLUME_BUDGET_FIFO"


class AcceptancePolicy(StrEnum):
    ACCEPT_SUPPORTED = "ACCEPT_SUPPORTED_OFFLINE"
    REJECT_ALL = "REJECT_ALL_MODELED"


@dataclass(frozen=True)
class SimulationPolicy:
    pricing: ExecutionPolicy
    acceptance_latency_ms: int
    execution_delay_ms: int
    cancellation_latency_ms: int
    delivery_latency_ms: int
    queue: QueuePolicy
    acceptance: AcceptancePolicy
    assumptions: str
    rules_version: str = "closed-minute-shared-budget-v1"

    def __post_init__(self) -> None:
        require_type(self.pricing, ExecutionPolicy, "pricing")
        require_type(self.queue, QueuePolicy, "queue")
        require_type(self.acceptance, AcceptancePolicy, "acceptance")
        nonblank(self.assumptions, "assumptions")
        if self.rules_version != "closed-minute-shared-budget-v1":
            raise ValueError("Versión de simulación no implementada")
        for name in (
            "acceptance_latency_ms",
            "execution_delay_ms",
            "cancellation_latency_ms",
            "delivery_latency_ms",
        ):
            value = getattr(self, name)
            minimum = 0 if name == "delivery_latency_ms" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(name + " requiere int y transición temporal positiva")
            try:
                timedelta(milliseconds=value)
            except OverflowError as exc:
                raise ValueError(name + " no representable") from exc

    @property
    def identity(self) -> str:
        return "simulated-policy-v1:" + sha256(encoded(self)).hexdigest()
