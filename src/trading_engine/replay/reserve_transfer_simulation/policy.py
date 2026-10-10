"""Hipótesis explícitas de partición y latencia; ninguna regla oficial del exchange."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256

from trading_engine.models._validation import finite_decimal, require_type
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_performance.evidence import Origin, Reference


@dataclass(frozen=True, kw_only=True)
class AutoMarginPolicy:
    trigger_buffer_usdc: Decimal
    target_buffer_usdc: Decimal
    minimum_transfer_usdc: Decimal
    maximum_transfer_usdc: Decimal
    max_per_round_usdc: Decimal
    max_simultaneous_requests: int
    request_latency_ms: int
    confirmation_latency_ms: int
    timeout_ms: int
    retry_policy: str
    rounding_policy: str
    source_reference: Reference
    enabled: bool = False

    def __post_init__(self) -> None:
        require_type(self.enabled, bool, "enabled")
        require_type(self.source_reference, Reference, "source_reference")
        if self.source_reference.provenance is not Origin.MODELED:
            raise ValueError("Política de simulación requiere MODELED")
        for name in (
            "trigger_buffer_usdc",
            "target_buffer_usdc",
            "minimum_transfer_usdc",
            "maximum_transfer_usdc",
            "max_per_round_usdc",
        ):
            finite_decimal(getattr(self, name), name, nonnegative=True)
        if not 0 < self.minimum_transfer_usdc <= self.maximum_transfer_usdc:
            raise ValueError("Límites de transferencia inválidos")
        if self.target_buffer_usdc < self.trigger_buffer_usdc or self.max_per_round_usdc <= 0:
            raise ValueError("Target/capacidad de ronda inválidos")
        for name in (
            "max_simultaneous_requests",
            "request_latency_ms",
            "confirmation_latency_ms",
            "timeout_ms",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError("Tiempo/cantidad requiere int no negativo")
        if self.max_simultaneous_requests < 1 or self.timeout_ms < 1:
            raise ValueError("Capacidad y timeout deben ser positivos")
        if self.request_latency_ms + self.confirmation_latency_ms < 1:
            raise ValueError("Confirmación requiere latencia total positiva")
        try:
            for value in (self.request_latency_ms, self.confirmation_latency_ms, self.timeout_ms):
                timedelta(milliseconds=value)
        except OverflowError as exc:
            raise ValueError("Latencia no representable") from exc
        if self.retry_policy != "NO_RETRY" or self.rounding_policy != "EXACT_DECIMAL":
            raise ValueError("Sólo NO_RETRY y EXACT_DECIMAL están implementados")


@dataclass(frozen=True)
class ReservationPolicy:
    source_reference: Reference
    market_price_policy: str
    assumption: str = "REMAINING_NOTIONAL_OVER_LEVERAGE"
    allocation: str = "RECONCILED_WALLET_DELTAS"
    timestamp_rule: str = "FACTS_COSTS_HOLDS_THEN_CONFIRM_THEN_DECIDE"

    def __post_init__(self) -> None:
        require_type(self.source_reference, Reference, "source_reference")
        if self.source_reference.provenance is not Origin.MODELED:
            raise ValueError("Retenciones requieren hipótesis MODELED")
        if self.market_price_policy not in {"EXPLICIT_PRICE_EVIDENCE", "CAUSAL_FRESH_MARK"}:
            raise ValueError("Referencia MARKET debe ser explícita")
        if self.assumption != "REMAINING_NOTIONAL_OVER_LEVERAGE":
            raise ValueError("Hipótesis de hold no implementada")
        if self.allocation != "RECONCILED_WALLET_DELTAS":
            raise ValueError("Partición no implementada")
        if self.timestamp_rule != "FACTS_COSTS_HOLDS_THEN_CONFIRM_THEN_DECIDE":
            raise ValueError("Prioridad económica no implementada")

    @property
    def identity(self) -> str:
        return sha256(encoded(self)).hexdigest()
