"""Cálculos teóricos a 50 dígitos, sin cuantización ni contexto global."""

from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

from trading_engine.models._validation import require_type
from trading_engine.models.orders import Fill
from trading_engine.models.values import Price
from trading_engine.strategy.alt_b.config import AltBConfig

MATH_PRECISION = 50


def _context() -> Context:
    # Context nuevo: no hereda precisión, redondeo, flags ni traps del llamador.
    return Context(
        prec=MATH_PRECISION,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def order_weights(config: AltBConfig) -> tuple[Decimal, ...]:
    """Base y DCA1..7, derivados de los pesos y multiplicador canónicos."""
    require_type(config, AltBConfig, "config")
    with localcontext(_context()):
        return (config.base_weight,) + tuple(
            config.dca1_weight * config.amount_multiplier**j
            for j in range(config.additional_dca_orders)
        )


def order_margins(config: AltBConfig) -> tuple[Decimal, ...]:
    """Distribuye el presupuesto por fórmula, sin ajustar un nivel residual."""
    weights = order_weights(config)
    with localcontext(_context()):
        total_weight = sum(weights, Decimal("0"))
        return tuple(config.order_budget * weight / total_weight for weight in weights)


def order_notionals(config: AltBConfig) -> tuple[Decimal, ...]:
    """Margen por leverage una sola vez; no deriva cantidades ejecutables."""
    margins = order_margins(config)
    with localcontext(_context()):
        return tuple(margin * config.leverage for margin in margins)


def dca_cumulative_deviation(level: int, config: AltBConfig) -> Decimal:
    require_type(config, AltBConfig, "config")
    if type(level) is not int:
        raise TypeError("level debe ser int")
    if not 1 <= level <= config.additional_dca_orders:
        raise ValueError("level debe estar entre 1 y 7")
    with localcontext(_context()):
        return config.first_deviation * sum(
            (config.deviation_multiplier**j for j in range(level)), Decimal("0")
        )


def dca_theoretical_price(p0: Price, level: int, config: AltBConfig) -> Price:
    """Cada nivel se ancla directamente a P0, nunca al DCA anterior."""
    require_type(p0, Price, "p0")
    deviation = dca_cumulative_deviation(level, config)
    with localcontext(_context()):
        return Price(p0.value * (Decimal("1") - deviation))


def weighted_average_fill_price(fills: tuple[Fill, ...]) -> Price:
    """El llamador selecciona los fills de entrada; no se clasifican aquí."""
    require_type(fills, tuple, "fills")
    if not fills:
        raise ValueError("fills no puede estar vacío")
    for fill in fills:
        require_type(fill, Fill, "fill")
    with localcontext(_context()):
        total_value = sum((fill.quantity.value * fill.price.value for fill in fills), Decimal("0"))
        total_quantity = sum((fill.quantity.value for fill in fills), Decimal("0"))
        return Price(total_value / total_quantity)


def take_profit_theoretical_price(average_entry_price: Price, config: AltBConfig) -> Price:
    """Movimiento de precio del 0.80%; sin leverage, fees ni funding."""
    require_type(average_entry_price, Price, "average_entry_price")
    require_type(config, AltBConfig, "config")
    with localcontext(_context()):
        return Price(average_entry_price.value * (Decimal("1") + config.take_profit_fraction))
