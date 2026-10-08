"""Snapshot inmutable: solo símbolo y presupuesto son configurables."""

from dataclasses import dataclass
from decimal import Decimal

from trading_engine.models._validation import finite_decimal, require_type
from trading_engine.models.values import Symbol


@dataclass(frozen=True)
class AltBConfig:
    strategy_id: str = "B_DCA7_4X_TP080_AMPLIADO_VPS"
    symbol: Symbol = Symbol("1000SHIBUSDC")
    order_budget: Decimal = Decimal("1200")
    leverage: int = 4
    additional_dca_orders: int = 7
    take_profit_fraction: Decimal = Decimal("0.008")
    first_deviation: Decimal = Decimal("0.015")
    deviation_multiplier: Decimal = Decimal("1.15")
    amount_multiplier: Decimal = Decimal("1.40")
    base_weight: Decimal = Decimal("59")
    dca1_weight: Decimal = Decimal("69")
    strategy_stop: bool = False
    trailing: bool = False
    reinvest_order_sizes: bool = False
    change_24h_min: Decimal = Decimal("-0.045")
    change_24h_max: Decimal = Decimal("0.08")
    range_4h_min: Decimal = Decimal("0.022")
    pullback_1h_min: Decimal = Decimal("0.011")
    veto_a_volume_multiplier: Decimal = Decimal("8")
    veto_a_drop_1h_min: Decimal = Decimal("0.01")
    veto_b_buy_ratio_max: Decimal = Decimal("0.25")
    veto_b_volume_multiplier: Decimal = Decimal("2.5")
    veto_b_rv_multiplier: Decimal = Decimal("1.5")
    warmup_minutes: int = 1560

    def __post_init__(self) -> None:
        require_type(self.strategy_id, str, "strategy_id")
        if self.strategy_id != "B_DCA7_4X_TP080_AMPLIADO_VPS":
            raise ValueError("strategy_id debe conservar el valor canónico")
        require_type(self.symbol, Symbol, "symbol")
        finite_decimal(self.order_budget, "order_budget")
        if self.order_budget <= 0:
            raise ValueError("order_budget debe ser positivo")

        for name, int_value, int_expected in (
            ("leverage", self.leverage, 4),
            ("additional_dca_orders", self.additional_dca_orders, 7),
            ("warmup_minutes", self.warmup_minutes, 1560),
        ):
            if type(int_value) is not int:
                raise TypeError(f"{name} debe ser int")
            if int_value != int_expected:
                raise ValueError(f"{name} debe conservar el valor canónico")

        for name, decimal_value, decimal_expected in (
            ("take_profit_fraction", self.take_profit_fraction, Decimal("0.008")),
            ("first_deviation", self.first_deviation, Decimal("0.015")),
            ("deviation_multiplier", self.deviation_multiplier, Decimal("1.15")),
            ("amount_multiplier", self.amount_multiplier, Decimal("1.40")),
            ("base_weight", self.base_weight, Decimal("59")),
            ("dca1_weight", self.dca1_weight, Decimal("69")),
        ):
            finite_decimal(decimal_value, name)
            if decimal_value <= 0 or decimal_value != decimal_expected:
                raise ValueError(f"{name} debe conservar el valor canónico positivo")

        for name, decimal_value, decimal_expected in (
            ("change_24h_min", self.change_24h_min, Decimal("-0.045")),
            ("change_24h_max", self.change_24h_max, Decimal("0.08")),
            ("range_4h_min", self.range_4h_min, Decimal("0.022")),
            ("pullback_1h_min", self.pullback_1h_min, Decimal("0.011")),
            ("veto_a_volume_multiplier", self.veto_a_volume_multiplier, Decimal("8")),
            ("veto_a_drop_1h_min", self.veto_a_drop_1h_min, Decimal("0.01")),
            ("veto_b_buy_ratio_max", self.veto_b_buy_ratio_max, Decimal("0.25")),
            ("veto_b_volume_multiplier", self.veto_b_volume_multiplier, Decimal("2.5")),
            ("veto_b_rv_multiplier", self.veto_b_rv_multiplier, Decimal("1.5")),
        ):
            finite_decimal(decimal_value, name)
            if decimal_value != decimal_expected:
                raise ValueError(f"{name} debe conservar el valor canónico")

        for name, bool_value in (
            ("strategy_stop", self.strategy_stop),
            ("trailing", self.trailing),
            ("reinvest_order_sizes", self.reinvest_order_sizes),
        ):
            require_type(bool_value, bool, name)
            if bool_value:
                raise ValueError(f"{name} debe ser False")
