"""Valores exactos e inmutables; sin redondeo ni conversión de divisas."""

from dataclasses import dataclass
from decimal import Decimal

from trading_engine.models._validation import finite_decimal, normalized_code


@dataclass(frozen=True)
class Symbol:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", normalized_code(self.value, "symbol"))


@dataclass(frozen=True)
class Price:
    value: Decimal

    def __post_init__(self) -> None:
        finite_decimal(self.value, "price")
        if self.value <= 0:
            raise ValueError("price debe ser positivo")


@dataclass(frozen=True)
class Quantity:
    value: Decimal

    def __post_init__(self) -> None:
        finite_decimal(self.value, "quantity")
        if self.value <= 0:
            raise ValueError("quantity debe ser positiva")


@dataclass(frozen=True)
class Money:
    """Importe firmado: admite cero y negativos, siempre ligado a una moneda."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        finite_decimal(self.amount, "amount")
        object.__setattr__(self, "currency", normalized_code(self.currency, "currency"))
