"""Raíces de sensibilidad por tramo; no precio oficial de liquidación."""

from decimal import Decimal, localcontext

from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance.core import metric
from trading_engine.replay.financial_performance.evidence import Origin
from trading_engine.replay.financial_performance.models import Metric
from trading_engine.replay.isolated_margin_risk.evidence import (
    MaintenanceBracket,
    MaintenanceBracketSchedule,
)


def threshold(
    quantity: Decimal,
    cost: Decimal,
    wallet: Metric,
    schedule: MaintenanceBracketSchedule | None,
    *,
    enabled: bool,
    leverage: Decimal,
) -> Metric:
    missing = metric(None, (), "THRESHOLD_POLICY_INPUT_OR_VERIFIABLE_ROOT_MISSING")
    if not enabled or quantity <= 0 or wallet.value is None or schedule is None:
        return missing
    balance = wallet.value
    with localcontext(ledger_context()) as ctx:
        roots: list[Decimal] = []
        for bracket in schedule.brackets:
            if bracket.max_leverage is not None and leverage > bracket.max_leverage:
                continue
            # W + Q*p - C = Q*p*r - cum; W se mantiene bajo la hipótesis.
            root = (cost - wallet.value - bracket.maintenance_amount) / (
                quantity * (Decimal("1") - bracket.maintenance_rate)
            )
            if root <= 0 or not bracket.contains(quantity * root):
                continue

            def residual(price: Decimal, rule: MaintenanceBracket = bracket) -> Decimal:
                # Forma lineal equivalente: evita cancelación numérica al
                # restar dos balances grandes cerca de una raíz periódica.
                return quantity * (Decimal("1") - rule.maintenance_rate) * price - (
                    cost - balance - rule.maintenance_amount
                )

            # Validación a la resolución Decimal50: signos a ambos vecinos
            # representables, sin tolerancia económica arbitraria.
            low, high = ctx.next_minus(root), ctx.next_plus(root)
            if residual(root) != 0 and not (
                bracket.contains(quantity * low)
                and bracket.contains(quantity * high)
                and residual(low) <= 0 <= residual(high)
            ):
                continue
            roots.append(root)
        if len(set(roots)) != 1:
            return missing
        return metric(
            roots[0],
            wallet.provenances + (schedule.stamp.reference.provenance, Origin.MODELED),
            "FIXED_WALLET_PIECEWISE_SENSITIVITY_NOT_OFFICIAL_LIQUIDATION_PRICE",
        )
