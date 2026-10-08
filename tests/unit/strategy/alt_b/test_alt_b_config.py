from dataclasses import FrozenInstanceError, asdict, fields, replace
from decimal import Decimal

import pytest

from trading_engine.models.values import Symbol
from trading_engine.strategy.alt_b.config import AltBConfig

DECIMAL_FIELDS = (
    "take_profit_fraction",
    "first_deviation",
    "deviation_multiplier",
    "amount_multiplier",
    "base_weight",
    "dca1_weight",
)
INT_FIELDS = ("leverage", "additional_dca_orders")
BOOL_FIELDS = ("strategy_stop", "trailing", "reinvest_order_sizes")


def test_defaults_are_the_complete_canonical_snapshot() -> None:
    config = AltBConfig()
    assert asdict(config) == {
        "strategy_id": "B_DCA7_4X_TP080_AMPLIADO_VPS",
        "symbol": {"value": "1000SHIBUSDC"},
        "order_budget": Decimal("1200"),
        "leverage": 4,
        "additional_dca_orders": 7,
        "take_profit_fraction": Decimal("0.008"),
        "first_deviation": Decimal("0.015"),
        "deviation_multiplier": Decimal("1.15"),
        "amount_multiplier": Decimal("1.40"),
        "base_weight": Decimal("59"),
        "dca1_weight": Decimal("69"),
        "strategy_stop": False,
        "trailing": False,
        "reinvest_order_sizes": False,
    }


@pytest.mark.parametrize("budget", ["1200", "2000", "3200", "0.000000000000000000001", "1e-1000"])
def test_budget_and_symbol_are_configurable_without_conversion(budget: str) -> None:
    amount = Decimal(budget)
    symbol = Symbol("OTHERUSDC")
    config = AltBConfig(symbol=symbol, order_budget=amount)
    assert config.symbol is symbol
    assert config.order_budget is amount


@pytest.mark.parametrize("field", ["order_budget", *DECIMAL_FIELDS])
@pytest.mark.parametrize(
    "value",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_decimal_fields_reject_nonpositive_or_nonfinite(field: str, value: Decimal) -> None:
    with pytest.raises(ValueError):
        replace(AltBConfig(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["order_budget", *DECIMAL_FIELDS])
@pytest.mark.parametrize("value", [0.008, 1200, True, False, "1200", None])
def test_decimal_fields_reject_non_decimal(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        replace(AltBConfig(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", DECIMAL_FIELDS)
def test_each_decimal_parameter_rejects_a_different_positive_value(field: str) -> None:
    with pytest.raises(ValueError):
        replace(AltBConfig(), **{field: Decimal("2")})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", INT_FIELDS)
@pytest.mark.parametrize("value", [True, False, 4.0, Decimal("4"), "4", None])
def test_integer_parameters_require_real_int(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        replace(AltBConfig(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", INT_FIELDS)
@pytest.mark.parametrize("value", [0, -1, 5, 8])
def test_integer_parameters_reject_noncanonical_int(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        replace(AltBConfig(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("value", [0, 1, "False", Decimal("0"), None])
def test_boolean_parameters_require_real_bool(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        replace(AltBConfig(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", BOOL_FIELDS)
def test_boolean_parameters_cannot_be_enabled(field: str) -> None:
    with pytest.raises(ValueError):
        replace(AltBConfig(), **{field: True})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["OTHER", "", " "])
def test_strategy_identity_cannot_be_changed(value: str) -> None:
    with pytest.raises(ValueError):
        replace(AltBConfig(), strategy_id=value)


@pytest.mark.parametrize("value", [1, True, None])
def test_strategy_identity_requires_string(value: object) -> None:
    with pytest.raises(TypeError):
        replace(AltBConfig(), strategy_id=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["1000SHIBUSDC", 1, None])
def test_symbol_requires_domain_symbol(value: object) -> None:
    with pytest.raises(TypeError):
        replace(AltBConfig(), symbol=value)  # type: ignore[arg-type]


def test_every_config_field_is_frozen() -> None:
    config = AltBConfig()
    original = asdict(config)
    for field in fields(config):
        with pytest.raises(FrozenInstanceError):
            setattr(config, field.name, getattr(config, field.name))
    assert asdict(config) == original
