"""Validación runtime compartida por valores y snapshots de dominio."""

from datetime import datetime
from decimal import Decimal

from trading_engine.utils.time import require_utc


def require_type[T](value: object, expected: type[T], name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} debe ser {expected.__name__}")


def finite_decimal(value: Decimal, name: str, *, nonnegative: bool = False) -> None:
    require_type(value, Decimal, name)
    if not value.is_finite():
        raise ValueError(f"{name} debe ser finito")
    if nonnegative and value < 0:
        raise ValueError(f"{name} no puede ser negativo")


def utc_timestamp(value: datetime, name: str) -> None:
    require_type(value, datetime, name)
    require_utc(value)


def nonblank(value: str, name: str) -> None:
    require_type(value, str, name)
    if not value.strip():
        raise ValueError(f"{name} no puede estar vacío")


def normalized_code(value: str, name: str) -> str:
    nonblank(value, name)
    if any(character.isspace() for character in value):
        raise ValueError(f"{name} no puede contener espacios")
    return value.upper()
