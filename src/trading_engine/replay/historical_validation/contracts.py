"""Identidad contractual, disponibilidad y límites explícitos de auditoría."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, InvalidOperation
from enum import StrEnum

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, MARKET, minute_ms
from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.values import Symbol
from trading_engine.utils.time import require_utc


class DatasetType(StrEnum):
    TRADE = "TRADE_KLINES_1M"
    MARK = "MARK_PRICE_KLINES_1M"
    FUNDING = "FUNDING_RATE_HISTORY"
    SYMBOL_RULES = "SYMBOL_RULES"
    BRACKETS = "MAINTENANCE_BRACKETS"
    FEES = "TRADING_FEE_RULES"
    EXECUTION = "ORDER_EXECUTION_EVIDENCE"
    CAPITAL = "CAPITAL_AND_MARGIN_EVIDENCE"
    HIGH_FREQUENCY = "HIGH_FREQUENCY_MARK_OR_TRADES"


class Authentication(StrEnum):
    DECLARED = "SOURCE_DECLARED"
    AUTHENTICATED = "SOURCE_AUTHENTICATED"
    UNKNOWN = "NOT_AUTHENTICATED"


class Readiness(StrEnum):
    READY = "READY_FOR_VALIDATION"
    INCOMPLETE = "INCOMPLETE"
    MISSING = "MISSING"
    CONFLICTED = "CONFLICTED"
    UNAUTHENTICATED = "NOT_AUTHENTICATED"
    NONCAUSAL = "NOT_CAUSALLY_USABLE"


@dataclass(frozen=True)
class Contract:
    exchange: str = "BINANCE"
    market: str = MARKET
    symbol: Symbol = DEFAULT_SYMBOL
    contract_type: str = "PERPETUAL"
    settlement_asset: str = "USDC"

    def __post_init__(self) -> None:
        require_type(self.symbol, Symbol, "symbol")
        if (
            self.exchange != "BINANCE"
            or self.market != MARKET
            or self.symbol != DEFAULT_SYMBOL
            or self.contract_type != "PERPETUAL"
            or self.settlement_asset != "USDC"
        ):
            raise ValueError("Contrato requerido: BINANCE USDⓈ-M 1000SHIBUSDC PERPETUAL USDC")


@dataclass(frozen=True)
class Limits:
    """Límites de recursos locales; no son límites de mercado ni de trading."""

    max_file_bytes: int = 128 * 1024 * 1024
    max_uncompressed_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: int = 100
    max_line_bytes: int = 64 * 1024
    max_json_bytes: int = 1024 * 1024
    max_rows: int = 1_000_000
    max_findings: int = 1000
    max_months: int = 120

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name}: entero positivo requerido")


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class DatasetDeclaration:
    dataset_id: str
    dataset_type: DatasetType
    contract: Contract
    interval: str
    period_start: datetime
    period_end: datetime
    source_url_declared: str
    source_documentation: str
    acquisition_time: datetime
    schema_version: str
    unit_contract: str

    def __post_init__(self) -> None:
        require_type(self.dataset_type, DatasetType, "dataset_type")
        require_type(self.contract, Contract, "contract")
        for name in (
            "dataset_id",
            "source_url_declared",
            "source_documentation",
            "schema_version",
            "unit_contract",
        ):
            nonblank(getattr(self, name), name)
        require_utc(self.period_start)
        require_utc(self.period_end)
        require_utc(self.acquisition_time)
        if self.period_start < LAUNCH or self.period_start >= self.period_end:
            raise ValueError("Dataset fuera de vigencia contractual")
        if self.schema_version != "historical-local-v1":
            raise ValueError("Schema no implementado")
        if self.unit_contract != units(self.dataset_type):
            raise ValueError("Unidades explícitas incompatibles; sin conversión por 1000")
        if self.dataset_type in {DatasetType.TRADE, DatasetType.MARK}:
            if self.interval != "1m":
                raise ValueError("Klines deben ser 1m")
            minute_ms(self.period_start)
            minute_ms(self.period_end)
        elif self.interval != "EVENTS":
            raise ValueError("Datos no kline requieren intervalo EVENTS")


def units(kind: DatasetType) -> str:
    return {
        DatasetType.TRADE: "price=USDC/1000SHIB;volume=1000SHIB;taker_buy=1000SHIB",
        DatasetType.MARK: "price=USDC/1000SHIB;sample=closed_interval_only",
        DatasetType.FUNDING: "rate=fraction;reference_price=USDC/1000SHIB;not_a_payment",
        DatasetType.SYMBOL_RULES: "price=USDC/1000SHIB;quantity=1000SHIB;notional=USDC",
        DatasetType.BRACKETS: "notional=USDC;maintenance_rate=fraction;cum=USDC",
        DatasetType.FEES: "rate=fraction;fee_currency=explicit;role=not_inferred",
        DatasetType.EXECUTION: "quantity=1000SHIB;price=USDC/1000SHIB;not_authorized",
        DatasetType.CAPITAL: "currency=USDC;partition=explicit;not_authorized",
        DatasetType.HIGH_FREQUENCY: "price=USDC/1000SHIB;kind=explicit;catalog_only",
    }[kind]


def exact_decimal(value: object, name: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 200:
        raise ValueError(f"{name}: texto Decimal explícito requerido")
    try:
        result = Decimal(value, context=Context())
    except InvalidOperation as exc:
        raise ValueError(f"{name}: Decimal inválido") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"{name}: Decimal no finito/negativo")
    if result.adjusted() < -1000 or result.adjusted() > 1000:
        raise ValueError(f"{name}: exponente fuera del límite de auditoría")
    return result


def utc_text(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp UTC textual requerido")
    return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
