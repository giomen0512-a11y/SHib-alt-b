"""Validación central de timestamps internos."""

from datetime import UTC, datetime, timedelta


def require_utc(value: datetime) -> datetime:
    """Rechaza timestamps naive o con offset distinto de cero y normaliza a UTC."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Se requiere un timestamp UTC timezone-aware")
    return value.astimezone(UTC)
