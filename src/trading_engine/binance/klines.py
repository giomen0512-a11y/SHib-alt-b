"""Normalización estricta de klines públicos USDⓈ-M, sin ejecución."""

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation

from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.utils.time import require_utc

MARKET = "BINANCE_USDM_FUTURES"
DEFAULT_SYMBOL = Symbol("1000SHIBUSDC")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
LAUNCH = datetime(2024, 3, 28, 7, tzinfo=UTC)
MINUTE_MS = 60_000
CSV_HEADER = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)


def validate_symbol(symbol: Symbol) -> None:
    if not isinstance(symbol, Symbol):
        raise TypeError("symbol debe ser Symbol")
    if not re.fullmatch(r"[A-Z0-9]{2,30}", symbol.value):
        raise ValueError("Símbolo Futures inválido")
    if symbol.value in {"SHIBUSDC", "SHIBUSDT", "1000SHIBUSDT"}:
        raise ValueError("Símbolo fuera del alcance aprobado")


def to_ms(value: datetime) -> int:
    delta = require_utc(value) - EPOCH
    if value.microsecond % 1000:
        raise ValueError("Timestamp no representable en milisegundos exactos")
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def from_ms(value: int) -> datetime:
    if type(value) is not int or not 946_684_800_000 <= value < 4_102_444_800_000:
        raise ValueError("Timestamp Futures debe estar en milisegundos (2000–2100)")
    return EPOCH + timedelta(milliseconds=value)


def reference_ms(value: datetime) -> int:
    """El reloj de referencia puede tener microsegundos; cutoff conservador en ms."""
    value = require_utc(value)
    return to_ms(value.replace(microsecond=value.microsecond // 1000 * 1000))


def minute_ms(value: datetime) -> int:
    result = to_ms(value)
    from_ms(result)
    if result % MINUTE_MS:
        raise ValueError("El límite debe estar alineado a minuto UTC")
    return result


def _integer(value: object, name: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise ValueError(f"{name}: se requiere entero exacto")


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name}: se requiere texto Decimal exacto")
    try:
        result = Decimal(value, context=Context())
    except InvalidOperation as exc:
        raise ValueError(f"{name}: Decimal inválido") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name}: debe ser finito y no negativo")
    return result


def normalize_kline(row: Sequence[object], symbol: Symbol, *, as_of: datetime) -> Candle:
    """El símbolo procede de la petición/archivo; cierre inclusivo → exclusivo."""
    validate_symbol(symbol)
    now_ms = reference_ms(as_of)
    if isinstance(row, (str, bytes)) or len(row) != 12:
        raise ValueError("Se requieren exactamente 12 campos Futures")
    opened = _integer(row[0], "open_time")
    closed = _integer(row[6], "close_time")
    open_time, _ = from_ms(opened), from_ms(closed)
    if opened % MINUTE_MS or closed != opened + MINUTE_MS - 1:
        raise ValueError("Intervalo Futures 1m inconsistente")
    if symbol == DEFAULT_SYMBOL and open_time < LAUNCH:
        raise ValueError("Vela anterior al lanzamiento del contrato")
    if opened + MINUTE_MS > now_ms:
        raise ValueError("Vela abierta o futura")
    values = {i: _decimal(row[i], f"campo {i}") for i in (1, 2, 3, 4, 5, 7, 9, 10, 11)}
    if _integer(row[8], "trades") < 0:
        raise ValueError("trades negativo")
    if values[10] > values[7]:
        raise ValueError("taker_buy_quote_volume supera quote_volume")
    return Candle(
        symbol,
        open_time,
        from_ms(opened + MINUTE_MS),
        Price(values[1]),
        Price(values[2]),
        Price(values[3]),
        Price(values[4]),
        values[5],
        values[9],
    )
