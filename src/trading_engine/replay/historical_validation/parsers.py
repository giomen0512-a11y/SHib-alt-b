"""Parsers de registros históricos; tiempos económicos separados de descarga."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_engine.binance.klines import (
    DEFAULT_SYMBOL,
    LAUNCH,
    MINUTE_MS,
    from_ms,
    normalize_kline,
)
from trading_engine.models.market import Candle
from trading_engine.models.values import Price

from .contracts import DatasetType, exact_decimal, utc_text
from .rules import HistoricalRule, parse_rule


@dataclass(frozen=True)
class MarkKline:
    market_effective_at: datetime
    market_available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    quality: str = "CLOSED_MARK_OHLC_1M"
    intraminute_path: str = "UNKNOWN_INTRAMINUTE_PATH"

    def close_sample(self, at: datetime) -> Decimal:
        from trading_engine.utils.time import require_utc

        if require_utc(at) < self.market_available_at:
            raise ValueError("Mark Close todavía no disponible")
        return self.close


@dataclass(frozen=True)
class FundingRate:
    market_effective_at: datetime
    market_available_at: datetime
    rate: Decimal
    mark_price: Decimal | None
    rate_type: str | None
    economic_use: str = "HISTORICAL_RATE_NOT_USER_PAYMENT"


HistoricalRecord = Candle | MarkKline | FundingRate | HistoricalRule


def milliseconds(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) < 17:
        return int(value)
    raise ValueError("Timestamp requiere entero ms exacto")


def record_times(row: object, kind: DatasetType) -> tuple[datetime, datetime]:
    """Lee sólo metadatos causales antes de decodificar los valores del prefijo."""
    if kind in {DatasetType.TRADE, DatasetType.MARK}:
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError("Kline requiere exactamente 12 columnas")
        effective = from_ms(milliseconds(row[0]))
        return effective, effective + timedelta(minutes=1)
    if not isinstance(row, dict):
        raise ValueError("Registro JSON object requerido")
    if kind is DatasetType.FUNDING:
        return from_ms(milliseconds(row["fundingTime"])), utc_text(row["market_available_at"])
    return utc_text(row["valid_from"]), utc_text(row["market_available_at"])


def parse_record(row: object, kind: DatasetType, *, as_of: datetime) -> HistoricalRecord:
    if isinstance(row, dict):
        expected = {
            "exchange": "BINANCE",
            "market": "BINANCE_USDM_FUTURES",
            "contract_type": "PERPETUAL",
            "settlement_asset": "USDC",
            "dataset_type": kind.value,
        }
        for name, value in expected.items():
            if name in row and row[name] != value:
                raise ValueError(f"{name}: campo contradictorio con contrato declarado")
    effective, available = record_times(row, kind)
    if effective < LAUNCH or available > as_of or available < effective:
        raise ValueError("Evento anterior al lanzamiento/futuro/disponibilidad inconsistente")
    if kind in {DatasetType.TRADE, DatasetType.MARK}:
        assert isinstance(row, list)
        if kind is DatasetType.TRADE:
            return normalize_kline(row, DEFAULT_SYMBOL, as_of=as_of)
        opened = milliseconds(row[0])
        closed = milliseconds(row[6])
        if opened % MINUTE_MS or closed != opened + MINUTE_MS - 1:
            raise ValueError("Intervalo Mark 1m inconsistente")
        prices = [exact_decimal(row[i], f"Mark {i}", nonnegative=True) for i in (1, 2, 3, 4)]
        # Reutiliza únicamente la invariante OHLC aprobada; no publica Candle de Trade.
        Candle(
            DEFAULT_SYMBOL,
            effective,
            available,
            Price(prices[0]),
            Price(prices[1]),
            Price(prices[2]),
            Price(prices[3]),
            Decimal(0),
        )
        return MarkKline(effective, available, prices[0], prices[1], prices[2], prices[3])
    assert isinstance(row, dict)
    if kind is DatasetType.FUNDING:
        if row.get("symbol") != DEFAULT_SYMBOL.value:
            raise ValueError("Funding de otro contrato")
        rate = exact_decimal(row["fundingRate"], "fundingRate")
        if rate.copy_abs() >= 1:
            raise ValueError("Tasa funding fuera de rango declarativo")
        raw_mark = row.get("markPrice")
        mark = None if raw_mark is None else exact_decimal(raw_mark, "markPrice")
        if mark is not None and mark <= 0:
            raise ValueError("Mark funding debe ser positivo")
        rate_type = row.get("rateType")
        if rate_type is not None and (not isinstance(rate_type, str) or not rate_type.strip()):
            raise ValueError("rateType inválido")
        return FundingRate(effective, available, rate, mark, rate_type)
    return parse_rule(row, kind)
