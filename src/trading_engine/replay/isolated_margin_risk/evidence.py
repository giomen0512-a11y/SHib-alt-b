"""Contratos offline con identidad, tiempo económico y disponibilidad independientes."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from trading_engine.models._validation import finite_decimal, nonblank, require_type
from trading_engine.models.ids import FillId, RoundId
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance.evidence import Origin, Reference
from trading_engine.utils.time import require_utc


@dataclass(frozen=True)
class RiskStamp:
    event_id: str
    effective_at: datetime
    received_at: datetime
    sequence: int
    reference: Reference
    version: str
    symbol: Symbol = Symbol("1000SHIBUSDC")
    currency: str = "USDC"

    def __post_init__(self) -> None:
        nonblank(self.event_id, "event_id")
        nonblank(self.version, "version")
        require_utc(self.effective_at)
        require_utc(self.received_at)
        require_type(self.reference, Reference, "reference")
        require_type(self.symbol, Symbol, "symbol")
        if self.symbol != Symbol("1000SHIBUSDC") or self.currency != "USDC":
            raise ValueError("Evidencia exige 1000SHIBUSDC/USDC")
        if self.effective_at > self.received_at:
            raise ValueError("Evidencia de riesgo futura respecto a recepción")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence debe ser entero no negativo")


@dataclass(frozen=True)
class IsolatedMarginEvidence:
    stamp: RiskStamp
    round_id: RoundId
    wallet_balance_usdc: Decimal
    initial_assigned_usdc: Decimal
    fill_ids: tuple[FillId, ...]
    included_fees_usdc: Decimal | None = None
    included_funding_usdc: Decimal | None = None

    def __post_init__(self) -> None:
        validate_round(self)
        finite_decimal(self.wallet_balance_usdc, "wallet_balance")
        finite_decimal(self.initial_assigned_usdc, "initial_assigned", nonnegative=True)
        require_type(self.fill_ids, tuple, "fill_ids")
        for fill_id in self.fill_ids:
            require_type(fill_id, UUID, "fill_id")
        if len(set(self.fill_ids)) != len(self.fill_ids):
            raise ValueError("FillIds de balance duplicados")
        for name in ("included_fees_usdc", "included_funding_usdc"):
            value = getattr(self, name)
            if value is not None:
                finite_decimal(value, name, nonnegative=name == "included_fees_usdc")


@dataclass(frozen=True)
class MarginAdjustmentEvidence:
    stamp: RiskStamp
    round_id: RoundId
    amount_usdc: Decimal
    kind: str
    fill_id: FillId | None = None

    def __post_init__(self) -> None:
        validate_round(self)
        finite_decimal(self.amount_usdc, "adjustment", nonnegative=True)
        if self.amount_usdc <= 0 or self.kind not in {"ADD", "WITHDRAW", "SELL_RELEASE"}:
            raise ValueError("Ajuste requiere tipo explícito y cantidad positiva")
        if (self.kind == "SELL_RELEASE") != (self.fill_id is not None):
            raise ValueError("SELL_RELEASE exige exclusivamente su FillId SELL")
        if self.fill_id is not None:
            require_type(self.fill_id, UUID, "fill_id")


@dataclass(frozen=True)
class MaintenanceBracket:
    notional_floor: Decimal
    notional_cap: Decimal
    maintenance_rate: Decimal
    maintenance_amount: Decimal
    max_leverage: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("notional_floor", "notional_cap", "maintenance_rate", "maintenance_amount"):
            finite_decimal(getattr(self, name), name, nonnegative=True)
        if self.notional_floor >= self.notional_cap or self.maintenance_rate >= 1:
            raise ValueError("Rango o tasa de mantenimiento inválido")
        if self.max_leverage is not None:
            finite_decimal(self.max_leverage, "max_leverage")
            if self.max_leverage <= 0:
                raise ValueError("max_leverage debe ser positivo")
        with localcontext(ledger_context()):
            if self.notional_floor * self.maintenance_rate - self.maintenance_amount < 0:
                raise ValueError("Mantenimiento negativo dentro del bracket")

    def contains(self, notional: Decimal) -> bool:
        return self.notional_floor <= notional < self.notional_cap


@dataclass(frozen=True)
class MaintenanceBracketSchedule:
    stamp: RiskStamp
    valid_from: datetime
    valid_until: datetime
    brackets: tuple[MaintenanceBracket, ...]
    boundaries: str = "FLOOR_INCLUSIVE_CAP_EXCLUSIVE"
    formula: str = "NOTIONAL_TIMES_RATE_MINUS_CUM"

    def __post_init__(self) -> None:
        require_type(self.stamp, RiskStamp, "stamp")
        require_utc(self.valid_from)
        require_utc(self.valid_until)
        require_type(self.brackets, tuple, "brackets")
        if self.valid_from >= self.valid_until or not self.brackets:
            raise ValueError("Schedule sin vigencia/brackets")
        if self.boundaries != "FLOOR_INCLUSIVE_CAP_EXCLUSIVE":
            raise ValueError("Frontera no implementada")
        if self.formula != "NOTIONAL_TIMES_RATE_MINUS_CUM":
            raise ValueError("Fórmula no verificada por este contrato")
        previous = None
        for bracket in self.brackets:
            require_type(bracket, MaintenanceBracket, "bracket")
            if previous is not None and previous.notional_cap != bracket.notional_floor:
                raise ValueError("Brackets desordenados, discontinuos o solapados")
            previous = bracket


@dataclass(frozen=True)
class RiskValuationEvidence:
    stamp: RiskStamp
    round_id: RoundId
    additional_margin_usdc: Decimal | None = None
    target_buffer_usdc: Decimal | None = None
    reserve_available_usdc: Decimal | None = None
    transfer_assumed_possible: bool | None = None

    def __post_init__(self) -> None:
        validate_round(self)
        for name in ("additional_margin_usdc", "target_buffer_usdc", "reserve_available_usdc"):
            value = getattr(self, name)
            if value is not None:
                finite_decimal(value, name, nonnegative=True)
        if self.transfer_assumed_possible is not None:
            require_type(self.transfer_assumed_possible, bool, "transfer_assumed_possible")


def validate_round(
    value: IsolatedMarginEvidence | MarginAdjustmentEvidence | RiskValuationEvidence,
) -> None:
    require_type(value.stamp, RiskStamp, "stamp")
    require_type(value.round_id, UUID, "round_id")


RiskEvent = (
    IsolatedMarginEvidence
    | MarginAdjustmentEvidence
    | MaintenanceBracketSchedule
    | RiskValuationEvidence
)


class RiskSource(Protocol):
    @property
    def identity(self) -> str: ...

    def read(self, cursor: int, until: datetime) -> Iterator[RiskEvent]: ...


@dataclass(frozen=True)
class FixtureRiskSource:
    events: tuple[RiskEvent, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.events, tuple, "events")
        for event in self.events:
            if not isinstance(
                event,
                (
                    IsolatedMarginEvidence,
                    MarginAdjustmentEvidence,
                    MaintenanceBracketSchedule,
                    RiskValuationEvidence,
                ),
            ):
                raise TypeError("Evidencia de riesgo inválida")
            if event.stamp.reference.provenance is not Origin.FIXTURE:
                raise ValueError("Fuente fixture exige SYNTHETIC_FIXTURE")
        if len({e.stamp.sequence for e in self.events}) != len(self.events):
            raise ValueError("Secuencias de riesgo duplicadas")
        object.__setattr__(
            self,
            "events",
            tuple(sorted(self.events, key=lambda e: (e.stamp.received_at, e.stamp.sequence))),
        )

    @property
    def identity(self) -> str:
        return "fixture-risk-v1:" + sha256(encoded(self.events)).hexdigest()

    def read(self, cursor: int, until: datetime) -> Iterator[RiskEvent]:
        for event in self.events[cursor:]:
            if event.stamp.received_at > until:
                break
            yield event
