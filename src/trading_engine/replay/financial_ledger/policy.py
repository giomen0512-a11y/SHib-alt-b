"""Política contable explícita, versionada y sin parámetros de mercado."""

from dataclasses import dataclass
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from enum import StrEnum
from hashlib import sha256

from trading_engine.models._validation import require_type
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded


class ExecutionOrigin(StrEnum):
    MODELED = "MODELED_EXECUTION"
    FIXTURE = "SYNTHETIC_FIXTURE"
    OBSERVED = "REAL_OBSERVED"


class EconomicOrdering(StrEnum):
    STRICT = "REJECT_LATE_USE_EXPLICIT_REPLAY_SEQUENCE_FOR_TIES"


def ledger_context() -> Context:
    return Context(
        prec=50,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


@dataclass(frozen=True)
class LedgerPolicy:
    symbol: Symbol = Symbol("1000SHIBUSDC")
    settlement_currency: str = "USDC"
    ordering: EconomicOrdering = EconomicOrdering.STRICT
    allowed_origins: tuple[ExecutionOrigin, ...] = (
        ExecutionOrigin.MODELED,
        ExecutionOrigin.FIXTURE,
    )
    rules_version: str = "moving-average-long-canonical-quantity-50-v1"

    def __post_init__(self) -> None:
        require_type(self.symbol, Symbol, "symbol")
        require_type(self.ordering, EconomicOrdering, "ordering")
        require_type(self.allowed_origins, tuple, "allowed_origins")
        if self.symbol != Symbol("1000SHIBUSDC") or self.settlement_currency != "USDC":
            raise ValueError("Contrato inicial del ledger: 1000SHIBUSDC/USDC")
        if self.rules_version != "moving-average-long-canonical-quantity-50-v1":
            raise ValueError("Versión contable no implementada")
        if not self.allowed_origins or len(set(self.allowed_origins)) != len(self.allowed_origins):
            raise ValueError("Procedencias permitidas vacías o duplicadas")
        for origin in self.allowed_origins:
            require_type(origin, ExecutionOrigin, "origin")
            if origin is ExecutionOrigin.OBSERVED:
                raise ValueError("REAL_OBSERVED requiere otro modo futuro; no habilitado")
        object.__setattr__(self, "allowed_origins", tuple(sorted(self.allowed_origins)))

    @property
    def identity(self) -> str:
        return "offline-financial-ledger-v1:" + sha256(encoded(self)).hexdigest()
