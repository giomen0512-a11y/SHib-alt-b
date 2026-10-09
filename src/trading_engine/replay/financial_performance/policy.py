"""Políticas explícitas de capital/comisiones; ningún valor de Binance implícito."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from trading_engine.models._validation import finite_decimal, nonblank, require_type
from trading_engine.models.ids import FillId
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_performance.evidence import Origin, Reference
from trading_engine.utils.time import require_utc


@dataclass(frozen=True)
class CapitalEvidence:
    balance_usdc: Decimal
    effective_at: datetime
    reference: Reference

    def __post_init__(self) -> None:
        finite_decimal(self.balance_usdc, "capital", nonnegative=True)
        require_utc(self.effective_at)
        require_type(self.reference, Reference, "reference")
        if self.reference.provenance is Origin.UNKNOWN:
            raise ValueError("Capital desconocido no autoriza patrimonio")


@dataclass(frozen=True)
class FeeRatePolicy:
    maker_rate: Decimal
    taker_rate: Decimal
    liquidity_by_fill: tuple[tuple[FillId, str], ...]
    reference: Reference

    def __post_init__(self) -> None:
        finite_decimal(self.maker_rate, "maker_rate", nonnegative=True)
        finite_decimal(self.taker_rate, "taker_rate", nonnegative=True)
        require_type(self.reference, Reference, "reference")
        if self.reference.provenance is not Origin.MODELED:
            raise ValueError("Tasas asumidas requieren etiqueta MODELED")
        require_type(self.liquidity_by_fill, tuple, "liquidity_by_fill")
        if len({i for i, _ in self.liquidity_by_fill}) != len(self.liquidity_by_fill):
            raise ValueError("Clasificación de liquidez duplicada")
        from uuid import UUID

        for fill_id, role in self.liquidity_by_fill:
            require_type(fill_id, UUID, "fill_id")
            if role not in {"MAKER", "TAKER"}:
                raise ValueError("Cada fill requiere rol explícito MAKER/TAKER")
        object.__setattr__(self, "liquidity_by_fill", tuple(sorted(self.liquidity_by_fill)))


@dataclass(frozen=True)
class PerformancePolicy:
    scenario: str
    assumptions: str
    capital: CapitalEvidence | None = None
    fees: FeeRatePolicy | None = None
    rules_version: str = "offline-financial-performance-v1"

    def __post_init__(self) -> None:
        if self.scenario not in {"FAVORABLE", "BASE", "ADVERSO"}:
            raise ValueError("Escenario financiero inválido")
        nonblank(self.assumptions, "assumptions")
        if self.capital is not None:
            require_type(self.capital, CapitalEvidence, "capital")
        if self.fees is not None:
            require_type(self.fees, FeeRatePolicy, "fees")
        if self.rules_version != "offline-financial-performance-v1":
            raise ValueError("Versión financiera no implementada")

    @property
    def identity(self) -> str:
        return "offline-financial-performance-v1:" + sha256(encoded(self)).hexdigest()
