"""Conversión exacta; sin políticas de precisión o redondeo de negocio."""

from decimal import Decimal


def to_decimal(value: Decimal | str | int) -> Decimal:
    """Rechaza float, bool y valores no finitos; no aplica cuantización."""
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise TypeError("Se requiere Decimal, str o int; float y bool no están permitidos")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Se requiere un Decimal finito")
    return result
