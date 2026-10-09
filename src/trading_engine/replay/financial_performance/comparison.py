"""Comparación reproducible de informes, sin declarar escenarios mercado observado."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from hashlib import sha256

from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance.models import Metric, PerformanceReport


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    identity: str
    fees: Metric
    funding: Metric
    net_cash: Metric
    wallet: Metric
    equity: Metric
    max_drawdown: Metric
    open_quantity: Decimal
    assumptions: str


@dataclass(frozen=True)
class ScenarioComparison:
    identity: str
    results: tuple[ScenarioResult, ...]
    scope: str = "SENSITIVITY_ONLY_NOT_OBSERVED_BINANCE"


def compare_scenarios(reports: tuple[PerformanceReport, ...]) -> ScenarioComparison:
    with localcontext(ledger_context()):
        return _compare_scenarios(reports)


def _compare_scenarios(reports: tuple[PerformanceReport, ...]) -> ScenarioComparison:
    scenarios = ("FAVORABLE", "BASE", "ADVERSO")
    if len(reports) != 3 or {r.policy.scenario for r in reports} != set(scenarios):
        raise ValueError("Comparación exige exactamente los tres escenarios")
    ordered = tuple(next(r for r in reports if r.policy.scenario == s) for s in scenarios)
    first = ordered[0]
    if any(
        (r.period_start, r.period_end, r.current.at, r.policy.capital)
        != (first.period_start, first.period_end, first.current.at, first.policy.capital)
        for r in ordered
    ):
        raise ValueError("Comparación requiere mismo período, valoración y capital")
    # Solo tasas directamente comparables: no ordenamos PnL ni precios futuros.
    rates = [r.policy.fees for r in ordered]
    if all(rate is not None for rate in rates):
        for a, b in zip(rates, rates[1:], strict=False):
            assert a is not None and b is not None
            if a.maker_rate > b.maker_rate or a.taker_rate > b.taker_rate:
                raise ValueError("Costos comparables incoherentes FAVORABLE→BASE→ADVERSO")
    results = tuple(
        ScenarioResult(
            r.policy.scenario,
            r.identity,
            r.current.fees,
            r.current.funding,
            r.current.net_cash,
            r.current.wallet,
            r.current.equity,
            r.max_drawdown_usdc,
            sum((position.exposure for position in r.current.rounds), Decimal("0")),
            r.policy.assumptions,
        )
        for r in ordered
    )
    return ScenarioComparison(
        "financial-sensitivity-v1:" + sha256(encoded(results)).hexdigest(), results
    )
