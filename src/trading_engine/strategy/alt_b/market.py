"""Intervalos UTC semiabiertos y estadísticas Decimal, sin completar datos."""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from trading_engine.models._validation import finite_decimal, require_type, utc_timestamp
from trading_engine.models.market import Candle
from trading_engine.strategy.alt_b.math import _context


def _validate_checkpoint(checkpoint_at: datetime) -> None:
    utc_timestamp(checkpoint_at, "checkpoint_at")
    if checkpoint_at.minute % 15 or checkpoint_at.second or checkpoint_at.microsecond:
        raise ValueError("checkpoint_at debe estar alineado a cierre UTC de 15m")


def _validate_candles(candles: tuple[Candle, ...]) -> None:
    require_type(candles, tuple, "candles")
    for candle in candles:
        require_type(candle, Candle, "candle")


def _consecutive_minutes(candles: tuple[Candle, ...], start: datetime) -> bool:
    return (
        all(
            candle.open_time == start + timedelta(minutes=index)
            and candle.close_time == candle.open_time + timedelta(minutes=1)
            and candle.symbol == candles[0].symbol
            for index, candle in enumerate(candles)
        )
        and start.second == start.microsecond == 0
    )


def aggregate_15m_block(candles: tuple[Candle, ...]) -> Candle:
    _validate_candles(candles)
    if len(candles) != 15:
        raise ValueError("Un bloque 15m requiere exactamente 15 velas")
    first = candles[0]
    if first.open_time.minute % 15 or not _consecutive_minutes(candles, first.open_time):
        raise ValueError("El bloque requiere 15 minutos consecutivos alineados a UTC 15m")
    with localcontext(_context()):
        taker_buy = (
            sum(
                (c.taker_buy_base_volume for c in candles if c.taker_buy_base_volume is not None),
                Decimal("0"),
            )
            if all(c.taker_buy_base_volume is not None for c in candles)
            else None
        )
        return Candle(
            first.symbol,
            first.open_time,
            candles[-1].close_time,
            first.open,
            max((c.high for c in candles), key=lambda p: p.value),
            min((c.low for c in candles), key=lambda p: p.value),
            candles[-1].close,
            sum((c.volume for c in candles), Decimal("0")),
            taker_buy,
        )


def decimal_median(values: tuple[Decimal, ...]) -> Decimal:
    require_type(values, tuple, "values")
    if not values:
        raise ValueError("La mediana requiere al menos un valor")
    for value in values:
        finite_decimal(value, "value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    with localcontext(_context()):
        return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def rv30m_snapshot(candles: tuple[Candle, ...], checkpoint_at: datetime) -> Decimal | None:
    """30 log returns / ddof=0; None si faltan los 31 cierres necesarios."""
    _validate_checkpoint(checkpoint_at)
    _validate_candles(candles)
    if any(c.close_time > checkpoint_at for c in candles):
        raise ValueError("No se admiten velas futuras")
    start = checkpoint_at - timedelta(minutes=31)
    window = tuple(c for c in candles if c.close_time > start)
    if len(window) != 31 or not _consecutive_minutes(window, start):
        return None
    with localcontext(_context()):
        returns = tuple(
            (current.close.value / previous.close.value).ln()
            for previous, current in zip(window, window[1:], strict=False)
        )
        mean = sum(returns, Decimal("0")) / Decimal("30")
        variance = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / Decimal("30")
        return variance.sqrt()
