"""Supuestos explícitos de sensibilidad; sin calibración ni ejecución de fills."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import cast

from trading_engine.models._validation import finite_decimal, nonblank, require_type
from trading_engine.replay.encoding import encoded


class Scenario(StrEnum):
    FAVORABLE = "FAVORABLE"
    BASE = "BASE"
    ADVERSE = "ADVERSO"


class LimitEligibility(StrEnum):
    TOUCH_CANDIDATE = "TOUCH_IS_CANDIDATE_ONLY"
    STRICT_CROSS_CANDIDATE = "STRICT_CROSS_IS_CANDIDATE_ONLY"
    ADDITIONAL_EVIDENCE = "REQUIRE_ADDITIONAL_EVIDENCE"


class PartialFillPolicy(StrEnum):
    EVIDENCE_REQUIRED = "ALLOW_PARTIAL_ONLY_WITH_EVIDENCE"
    WITHHOLD_IF_UNKNOWN = "WITHHOLD_IF_PARTIAL_REQUIREMENT_UNKNOWN"


class IntraminutePolicy(StrEnum):
    FAVORABLE_BOUND = "FAVORABLE_SENSITIVITY_BOUND"
    UNRESOLVED = "WITHHOLD_IF_SEQUENCE_UNKNOWN"
    ADVERSE_BOUND = "CONSERVATIVE_SENSITIVITY_BOUND"


def exact_decimal(value: Decimal, name: str) -> Decimal:
    finite_decimal(value, name, nonnegative=True)
    # Normalización exacta sin Decimal.normalize(), que podría heredar/recortar precisión.
    if value == 0:
        return Decimal("0")
    sign, digits, raw_exponent = value.as_tuple()
    exponent = cast(int, raw_exponent)
    while digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    return Decimal((sign, digits, exponent))


@dataclass(frozen=True)
class LiquidityPolicy:
    max_volume_participation: Decimal
    partial_fills: PartialFillPolicy

    def __post_init__(self) -> None:
        participation = exact_decimal(self.max_volume_participation, "max_volume_participation")
        if not 0 < participation <= 1:
            raise ValueError("Participación requiere 0 < p <= 1")
        require_type(self.partial_fills, PartialFillPolicy, "partial_fills")
        object.__setattr__(self, "max_volume_participation", participation)


@dataclass(frozen=True)
class ExecutionPolicy:
    scenario: Scenario
    spread_bps: Decimal
    slippage_bps: Decimal
    quote_latency_ms: int
    freshness_limit_ms: int
    limit_eligibility: LimitEligibility
    liquidity: LiquidityPolicy
    intraminute: IntraminutePolicy
    assumptions: str

    def __post_init__(self) -> None:
        require_type(self.scenario, Scenario, "scenario")
        spread = exact_decimal(self.spread_bps, "spread_bps")
        slippage = exact_decimal(self.slippage_bps, "slippage_bps")
        if spread >= 20000:
            raise ValueError("Spread completo debe ser < 20000 bps para bid positivo")
        if slippage >= 10000:
            raise ValueError("Slippage debe ser < 10000 bps para futuro precio SELL positivo")
        for name in ("quote_latency_ms", "freshness_limit_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(name + " requiere int no negativo")
            try:
                timedelta(milliseconds=value)
            except OverflowError as exc:
                raise ValueError(name + " no representable") from exc
        if self.freshness_limit_ms < self.quote_latency_ms:
            raise ValueError("Vigencia no puede ser menor que latencia")
        require_type(self.limit_eligibility, LimitEligibility, "limit_eligibility")
        require_type(self.liquidity, LiquidityPolicy, "liquidity")
        require_type(self.intraminute, IntraminutePolicy, "intraminute")
        expected = {
            Scenario.FAVORABLE: IntraminutePolicy.FAVORABLE_BOUND,
            Scenario.BASE: IntraminutePolicy.UNRESOLVED,
            Scenario.ADVERSE: IntraminutePolicy.ADVERSE_BOUND,
        }
        if self.intraminute is not expected[self.scenario]:
            raise ValueError("Política intraminuto incompatible con escenario")
        nonblank(self.assumptions, "assumptions")
        object.__setattr__(self, "spread_bps", spread)
        object.__setattr__(self, "slippage_bps", slippage)

    @property
    def identity(self) -> str:
        return "execution-policy-v1:" + sha256(encoded(self)).hexdigest()


@dataclass(frozen=True)
class ExecutionProfiles:
    profiles: tuple[ExecutionPolicy, ...]

    def __post_init__(self) -> None:
        require_type(self.profiles, tuple, "profiles")
        for profile in self.profiles:
            require_type(profile, ExecutionPolicy, "profile")
        if len(self.profiles) != 3 or {p.scenario for p in self.profiles} != set(Scenario):
            raise ValueError("Se requieren los tres escenarios, una vez cada uno")
        ordered = tuple(next(p for p in self.profiles if p.scenario is s) for s in Scenario)
        favorable, base, adverse = ordered
        for name in ("spread_bps", "slippage_bps", "quote_latency_ms"):
            if not getattr(favorable, name) <= getattr(base, name) <= getattr(adverse, name):
                raise ValueError("Costes no ordenados: " + name)
        if (
            not favorable.freshness_limit_ms
            >= base.freshness_limit_ms
            >= adverse.freshness_limit_ms
        ):
            raise ValueError("Vigencias no ordenadas por restricción")
        if (
            not favorable.liquidity.max_volume_participation
            >= base.liquidity.max_volume_participation
            >= adverse.liquidity.max_volume_participation
        ):
            raise ValueError("Participaciones no ordenadas por restricción")
        limits = list(LimitEligibility)
        partials = list(PartialFillPolicy)
        if (
            not limits.index(favorable.limit_eligibility)
            <= limits.index(base.limit_eligibility)
            <= limits.index(adverse.limit_eligibility)
        ):
            raise ValueError("Elegibilidad LIMIT no ordenada por restricción")
        if (
            not partials.index(favorable.liquidity.partial_fills)
            <= partials.index(base.liquidity.partial_fills)
            <= partials.index(adverse.liquidity.partial_fills)
        ):
            raise ValueError("Políticas parciales no ordenadas por restricción")
        object.__setattr__(self, "profiles", ordered)

    def select(self, scenario: Scenario) -> ExecutionPolicy:
        require_type(scenario, Scenario, "scenario")
        return next(p for p in self.profiles if p.scenario is scenario)

    @property
    def identity(self) -> str:
        return "execution-profiles-v1:" + sha256(encoded(self)).hexdigest()
