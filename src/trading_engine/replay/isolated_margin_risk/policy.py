"""Hipótesis explícitas del modelo; ninguna regla calibrada del exchange."""

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from trading_engine.models._validation import finite_decimal, nonblank, require_type
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_performance.evidence import Origin, Reference


@dataclass(frozen=True)
class MarginAllocationPolicy:
    initial: str
    release: str
    costs: str
    realized: str
    threshold_solver: bool
    linear_additional_margin: bool
    reference: Reference
    symbol: Symbol = Symbol("1000SHIBUSDC")
    currency: str = "USDC"
    leverage: Decimal = Decimal("4")
    direction: str = "LONG"
    margin_mode: str = "ISOLATED"
    position_mode: str = "ONE-WAY"
    rules_version: str = "isolated-long-50-v1"

    def __post_init__(self) -> None:
        require_type(self.reference, Reference, "reference")
        require_type(self.symbol, Symbol, "symbol")
        finite_decimal(self.leverage, "leverage")
        for name in ("threshold_solver", "linear_additional_margin"):
            require_type(getattr(self, name), bool, name)
        nonblank(self.reference.assumptions, "assumptions")
        if self.reference.provenance is Origin.UNKNOWN:
            raise ValueError("Política de asignación requiere hipótesis explícita conocida")
        if (
            self.symbol != Symbol("1000SHIBUSDC")
            or self.currency != "USDC"
            or self.leverage != Decimal("4")
            or (self.direction, self.margin_mode, self.position_mode)
            != ("LONG", "ISOLATED", "ONE-WAY")
            or self.rules_version != "isolated-long-50-v1"
        ):
            raise ValueError("Contrato canónico aislado LONG 1000SHIBUSDC/USDC 4x")
        choices = {
            "initial": {"EVIDENCE_ONLY", "EXECUTED_NOTIONAL_OVER_LEVERAGE"},
            "release": {"EXPLICIT_ONLY", "PROPORTIONAL_INITIAL_ON_SELL"},
            "costs": {"UNKNOWN", "RECONCILED_TO_ISOLATED", "INCLUDED_IN_BALANCE_EVIDENCE"},
            "realized": {"UNKNOWN", "TO_ISOLATED", "OUTSIDE_ISOLATED"},
        }
        for name, allowed in choices.items():
            if getattr(self, name) not in allowed:
                raise ValueError(f"Política no implementada: {name}")
        if self.initial == "EXECUTED_NOTIONAL_OVER_LEVERAGE":
            if self.reference.provenance is not Origin.MODELED:
                raise ValueError("Notional/leverage es siempre una hipótesis MODELED")
            if self.costs == "INCLUDED_IN_BALANCE_EVIDENCE":
                raise ValueError("Costos incluidos requieren balance explícito")
        elif self.release != "EXPLICIT_ONLY" or self.costs == "RECONCILED_TO_ISOLATED":
            raise ValueError(
                "Balances absolutos solo admiten liberación explícita/costos incluidos"
            )

    @property
    def identity(self) -> str:
        return "margin-allocation-v1:" + sha256(encoded(self)).hexdigest()
