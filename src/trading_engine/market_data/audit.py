"""Auditoría streaming; los rangos ausentes se conservan para recovery explícito."""

from dataclasses import dataclass
from datetime import datetime
from decimal import InvalidOperation

from trading_engine.binance.klines import MINUTE_MS, minute_ms, reference_ms, to_ms
from trading_engine.models.values import Symbol
from trading_engine.persistence.futures_sqlite import FuturesSQLite


@dataclass(frozen=True)
class Gap:
    start_ms: int
    end_ms: int

    @property
    def missing_minutes(self) -> int:
        return (self.end_ms - self.start_ms) // MINUTE_MS


@dataclass(frozen=True)
class AuditReport:
    start_ms: int
    end_ms: int
    rows: int
    gaps: tuple[Gap, ...]
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.rows > 0 and not self.gaps and not self.errors


def audit(
    store: FuturesSQLite, symbol: Symbol, start: datetime, end: datetime, *, as_of: datetime
) -> AuditReport:
    start_ms, end_ms = minute_ms(start), minute_ms(end)
    if start_ms >= end_ms or end_ms > reference_ms(as_of):
        raise ValueError("Rango de auditoría inválido o todavía abierto")
    expected, rows = start_ms, 0
    gaps: list[Gap] = []
    errors = list(store.integrity_errors()) + list(store.conflicts(symbol))
    try:
        for candle in store.candles(symbol, start_ms, end_ms):
            opened = to_ms(candle.open_time)
            if opened > expected:
                gaps.append(Gap(expected, opened))
            if opened < expected:
                errors.append(f"Duplicado/fuera de orden: {opened}")
            expected = opened + MINUTE_MS
            rows += 1
    except (ValueError, TypeError, InvalidOperation) as exc:
        errors.append(str(exc))
    if expected < end_ms:
        gaps.append(Gap(expected, end_ms))
    return AuditReport(start_ms, end_ms, rows, tuple(gaps), tuple(errors))
