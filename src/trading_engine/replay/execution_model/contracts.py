"""Contratos de sensibilidad futuros, sin autorizar ni generar fills."""

from dataclasses import dataclass, field
from datetime import datetime
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
from enum import StrEnum

from trading_engine.models._validation import require_type
from trading_engine.models.market import Candle
from trading_engine.models.values import Quantity, Symbol
from trading_engine.replay.execution_model.policy import IntraminutePolicy, LiquidityPolicy
from trading_engine.utils.time import require_utc


def price_context() -> Context:
    return Context(
        prec=50,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


class SequenceHypothesis(StrEnum):
    NONE = "NO_SEQUENCE_ASSUMED"
    TP_FIRST_BOUND = "TP_FIRST_SENSITIVITY_ONLY"
    DCA_FIRST_BOUND = "DCA_FIRST_SENSITIVITY_ONLY"


@dataclass(frozen=True)
class AmbiguityAssessment:
    ambiguous: bool
    hypothesis: SequenceHypothesis
    fill_authorized: bool = field(default=False, init=False)
    sequence_observed: bool = field(default=False, init=False)


def assess_ambiguity(
    policy: IntraminutePolicy, *, dca_candidate: bool, tp_candidate: bool
) -> AmbiguityAssessment:
    require_type(policy, IntraminutePolicy, "policy")
    require_type(dca_candidate, bool, "dca_candidate")
    require_type(tp_candidate, bool, "tp_candidate")
    ambiguous = dca_candidate and tp_candidate
    hypothesis = SequenceHypothesis.NONE
    if ambiguous:
        if policy is IntraminutePolicy.FAVORABLE_BOUND:
            hypothesis = SequenceHypothesis.TP_FIRST_BOUND
        elif policy is IntraminutePolicy.ADVERSE_BOUND:
            hypothesis = SequenceHypothesis.DCA_FIRST_BOUND
    return AmbiguityAssessment(ambiguous, hypothesis)


@dataclass(frozen=True)
class LiquidityAssessment:
    symbol: Symbol
    traded_base_volume: Decimal
    modeled_volume_cap: Decimal
    requested_quantity: Quantity
    within_volume_cap: bool
    queue_unknown: bool = field(default=True, init=False)
    price_depth_unknown: bool = field(default=True, init=False)
    fill_authorized: bool = field(default=False, init=False)


def assess_liquidity(
    candle: Candle, requested: Quantity, policy: LiquidityPolicy, *, at: datetime
) -> LiquidityAssessment:
    require_type(candle, Candle, "candle")
    require_type(requested, Quantity, "requested")
    require_type(policy, LiquidityPolicy, "policy")
    require_utc(at)
    if candle.close_time > at:
        raise ValueError("Volumen de vela aún no disponible")
    with localcontext(price_context()):
        cap = candle.volume * policy.max_volume_participation
    return LiquidityAssessment(candle.symbol, candle.volume, cap, requested, requested.value <= cap)
