"""Contratos locales temporales; no alimentan motores ni cambian redondeos."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH
from trading_engine.models._validation import nonblank, require_type
from trading_engine.replay.isolated_margin_risk.evidence import MaintenanceBracket

from .contracts import Authentication, DatasetType, exact_decimal, utc_text


class RuleQuality(StrEnum):
    VERIFIED = "VERIFIED_FOR_PERIOD"
    LAUNCH = "DOCUMENTED_AT_LAUNCH"
    CURRENT = "CURRENT_ONLY"
    UNKNOWN = "UNKNOWN_HISTORY"


@dataclass(frozen=True)
class RuleReference:
    valid_from: datetime
    valid_until: datetime
    market_available_at: datetime
    source: str
    source_version: str
    checksum: str | None
    authentication_status: Authentication
    quality: RuleQuality

    def __post_init__(self) -> None:
        from trading_engine.utils.time import require_utc

        for value in (self.valid_from, self.valid_until, self.market_available_at):
            require_utc(value)
        for name in ("source", "source_version"):
            nonblank(getattr(self, name), name)
        require_type(self.authentication_status, Authentication, "authentication_status")
        require_type(self.quality, RuleQuality, "quality")
        if self.valid_from < LAUNCH or self.valid_from >= self.valid_until:
            raise ValueError("Vigencia fuera de contrato")
        if self.authentication_status is Authentication.AUTHENTICATED:
            raise ValueError("Autenticidad requiere verificación independiente no habilitada")
        if self.checksum is not None:
            import re

            if not re.fullmatch("[a-f0-9]{64}", self.checksum):
                raise ValueError("Checksum de regla inválido")


@dataclass(frozen=True)
class SymbolRules:
    reference: RuleReference
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal
    min_price: Decimal
    max_price: Decimal
    max_leverage: Decimal
    contract_status: str


@dataclass(frozen=True)
class HistoricalBrackets:
    reference: RuleReference
    brackets: tuple[MaintenanceBracket, ...]
    user_adjustment_multiplier: Decimal | None
    user_scope: str | None


@dataclass(frozen=True)
class FeeRules:
    reference: RuleReference
    maker_rate: Decimal
    taker_rate: Decimal
    user_tier: str
    promotion: str
    discount: Decimal
    fee_currency: str
    effective_role_evidence: str


HistoricalRule = SymbolRules | HistoricalBrackets | FeeRules


def parse_rule(row: dict[str, object], kind: DatasetType) -> HistoricalRule:
    if row.get("symbol") != DEFAULT_SYMBOL.value or row.get("margin_asset") != "USDC":
        raise ValueError("Regla de otro símbolo/activo")
    for name in ("source", "source_version"):
        nonblank(cast(str, row[name]), name)
    ref = RuleReference(
        utc_text(row["valid_from"]),
        utc_text(row["valid_until"]),
        utc_text(row["market_available_at"]),
        cast(str, row["source"]),
        cast(str, row["source_version"]),
        cast(str | None, row.get("checksum")),
        Authentication(cast(str, row.get("authentication_status", Authentication.UNKNOWN))),
        RuleQuality(cast(str, row["quality"])),
    )
    if kind is DatasetType.SYMBOL_RULES:
        values = [
            exact_decimal(row[k], k, nonnegative=True)
            for k in (
                "tick_size",
                "step_size",
                "min_quantity",
                "max_quantity",
                "min_notional",
                "min_price",
                "max_price",
                "max_leverage",
            )
        ]
        if values[0] <= 0 or values[1] <= 0 or values[7] <= 0:
            raise ValueError("Tick/step/leverage no positivos")
        if values[2] > values[3] or values[5] > values[6]:
            raise ValueError("Filtros contradictorios")
        status = row["contract_status"]
        if not isinstance(status, str) or not status.strip():
            raise ValueError("Contract status desconocido")
        return SymbolRules(
            ref,
            values[0],
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
            values[6],
            values[7],
            status,
        )
    if kind is DatasetType.BRACKETS:
        raw = row["brackets"]
        if not isinstance(raw, list) or not raw or len(raw) > 100:
            raise ValueError("Brackets faltantes/excesivos")
        brackets: list[MaintenanceBracket] = []
        for bracket in raw:
            if not isinstance(bracket, dict):
                raise ValueError("Bracket inválido")
            values = [
                exact_decimal(bracket[k], k, nonnegative=True)
                for k in (
                    "notional_floor",
                    "notional_cap",
                    "maintenance_rate",
                    "maintenance_amount",
                    "max_leverage",
                )
            ]
            parsed = MaintenanceBracket(*values)
            if brackets and brackets[-1].notional_cap != parsed.notional_floor:
                raise ValueError("Brackets discontinuos/solapados")
            brackets.append(parsed)
        if brackets[0].notional_floor != 0:
            raise ValueError("Falta primer bracket")
        multiplier = row.get("user_adjustment_multiplier")
        scope = row.get("user_scope")
        if (multiplier is None) != (scope is None):
            raise ValueError("Ajuste específico exige multiplier y scope")
        adjusted = None if multiplier is None else exact_decimal(multiplier, "multiplier")
        if adjusted is not None and adjusted <= 0:
            raise ValueError("Multiplier no positivo")
        if scope is not None:
            nonblank(cast(str, scope), "user_scope")
        return HistoricalBrackets(ref, tuple(brackets), adjusted, cast(str | None, scope))
    if kind is DatasetType.FEES:
        maker = exact_decimal(row["maker_rate"], "maker_rate")
        taker = exact_decimal(row["taker_rate"], "taker_rate")
        discount = exact_decimal(row["discount"], "discount", nonnegative=True)
        if maker.copy_abs() >= 1 or taker.copy_abs() >= 1 or discount > 1:
            raise ValueError("Tarifa/descuento inválido")
        for name in ("user_tier", "promotion", "fee_currency", "effective_role_evidence"):
            nonblank(cast(str, row[name]), name)
        if row["fee_currency"] != "USDC":
            raise ValueError("Moneda de fee incompatible; conversión no implementada")
        return FeeRules(
            ref,
            maker,
            taker,
            str(row["user_tier"]),
            str(row["promotion"]),
            discount,
            "USDC",
            str(row["effective_role_evidence"]),
        )
    raise ValueError("Tipo de regla no implementado")
