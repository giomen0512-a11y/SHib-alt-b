"""Configuración y contratos de sensibilidad, sin modelo de fills."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from pathlib import Path
from typing import Any

import pytest

from trading_engine.models.market import Candle
from trading_engine.models.values import Quantity
from trading_engine.replay.execution_model.config import load_profiles, profiles_from_object
from trading_engine.replay.execution_model.contracts import (
    SequenceHypothesis,
    assess_ambiguity,
    assess_liquidity,
)
from trading_engine.replay.execution_model.policy import (
    ExecutionPolicy,
    ExecutionProfiles,
    IntraminutePolicy,
    LimitEligibility,
    LiquidityPolicy,
    PartialFillPolicy,
    Scenario,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "execution_profiles.json"


@pytest.fixture
def profiles() -> ExecutionProfiles:
    return load_profiles(FIXTURE_PATH)


def test_profiles_load_from_validated_file_and_object(profiles: ExecutionProfiles) -> None:
    assert profiles_from_object(json.loads(FIXTURE_PATH.read_text())) == profiles
    assert tuple(p.scenario for p in profiles.profiles) == tuple(Scenario)
    assert len({p.identity for p in profiles.profiles}) == 3
    assert profiles.identity == ExecutionProfiles(tuple(reversed(profiles.profiles))).identity
    assert all("FIXTURE" in p.assumptions for p in profiles.profiles)
    with pytest.raises(FrozenInstanceError):
        profiles.profiles = ()  # type: ignore[misc]


def test_canonical_identity_ignores_decimal_text_scale_and_global_context(
    profiles: ExecutionProfiles,
) -> None:
    policy = profiles.select(Scenario.BASE)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_UP
        ctx.traps[Inexact] = True
        same = replace(
            policy,
            spread_bps=Decimal("10.000"),
            slippage_bps=Decimal("3.00"),
            liquidity=replace(policy.liquidity, max_volume_participation=Decimal("0.050000")),
        )
        assert same.identity == policy.identity and same == policy
        assert ctx.prec == 2 and ctx.rounding == ROUND_UP


@pytest.mark.parametrize(
    "change",
    ["spread", "slippage", "latency", "freshness", "liquidity", "limit", "partials", "assumptions"],
)
def test_every_execution_assumption_changes_identity(
    profiles: ExecutionProfiles, change: str
) -> None:
    p = profiles.select(Scenario.BASE)
    variants = {
        "spread": replace(p, spread_bps=Decimal("11")),
        "slippage": replace(p, slippage_bps=Decimal("4")),
        "latency": replace(p, quote_latency_ms=251),
        "freshness": replace(p, freshness_limit_ms=90001),
        "liquidity": replace(
            p, liquidity=replace(p.liquidity, max_volume_participation=Decimal("0.04"))
        ),
        "limit": replace(p, limit_eligibility=LimitEligibility.ADDITIONAL_EVIDENCE),
        "partials": replace(
            p, liquidity=replace(p.liquidity, partial_fills=PartialFillPolicy.WITHHOLD_IF_UNKNOWN)
        ),
        "assumptions": replace(p, assumptions="Explicit alternative synthetic assumption"),
    }
    assert variants[change].identity != p.identity


@pytest.mark.parametrize(
    "field,value",
    [
        ("spread_bps", Decimal("-1")),
        ("spread_bps", Decimal("20000")),
        ("spread_bps", Decimal("NaN")),
        ("spread_bps", Decimal("Infinity")),
        ("spread_bps", 1.0),
        ("spread_bps", True),
        ("slippage_bps", Decimal("-1")),
        ("slippage_bps", Decimal("10000")),
        ("quote_latency_ms", -1),
        ("quote_latency_ms", True),
        ("quote_latency_ms", 1.0),
        ("freshness_limit_ms", 200),
        ("freshness_limit_ms", -1),
        ("freshness_limit_ms", 10**30),
        ("assumptions", ""),
        ("scenario", "BASE"),
        ("limit_eligibility", "automatic-fill"),
        ("intraminute", IntraminutePolicy.FAVORABLE_BOUND),
    ],
)
def test_invalid_policy_rejected(profiles: ExecutionProfiles, field: str, value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(profiles.select(Scenario.BASE), **{field: value})


@pytest.mark.parametrize(
    "participation", [Decimal("0"), Decimal("-0.1"), Decimal("1.001"), Decimal("NaN"), 0.1, True]
)
def test_invalid_liquidity_participation(participation: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        LiquidityPolicy(participation, PartialFillPolicy.EVIDENCE_REQUIRED)


@pytest.mark.parametrize(
    "case",
    [
        "spread",
        "slippage",
        "latency",
        "freshness",
        "participation",
        "limit",
        "partials",
        "missing",
        "duplicate",
    ],
)
def test_profiles_reject_unordered_or_incomplete_scenarios(
    profiles: ExecutionProfiles, case: str
) -> None:
    favorable, base, adverse = profiles.profiles
    values: tuple[ExecutionPolicy, ...]
    if case == "missing":
        values = (favorable, base)
    elif case == "duplicate":
        values = (favorable, base, base)
    else:
        changes = {
            "spread": replace(favorable, spread_bps=Decimal("20")),
            "slippage": replace(favorable, slippage_bps=Decimal("5")),
            "latency": replace(favorable, quote_latency_ms=500),
            "freshness": replace(favorable, freshness_limit_ms=70000),
            "participation": replace(
                favorable,
                liquidity=replace(favorable.liquidity, max_volume_participation=Decimal("0.01")),
            ),
            "limit": replace(favorable, limit_eligibility=LimitEligibility.ADDITIONAL_EVIDENCE),
            "partials": replace(
                favorable,
                liquidity=replace(
                    favorable.liquidity, partial_fills=PartialFillPolicy.WITHHOLD_IF_UNKNOWN
                ),
            ),
        }
        values = (changes[case], base, adverse)
    with pytest.raises(ValueError):
        ExecutionProfiles(values)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "extra",
        "decimal_number",
        "time_float",
        "time_bool",
        "version_bool",
        "version",
        "profiles",
        "bad_enum",
        "liquidity_missing",
        "secret",
    ],
)
def test_json_configuration_rejects_unknown_missing_or_unsafe_types(case: str) -> None:
    data = json.loads(FIXTURE_PATH.read_text())
    p = data["profiles"][1]
    if case == "missing":
        del p["spread_bps"]
    elif case == "extra":
        p["hidden_slippage"] = "5"
    elif case == "decimal_number":
        p["spread_bps"] = 1.0
    elif case == "time_float":
        p["quote_latency_ms"] = 0.1
    elif case == "time_bool":
        p["quote_latency_ms"] = True
    elif case == "version_bool":
        data["schema_version"] = True
    elif case == "version":
        data["schema_version"] = 2
    elif case == "profiles":
        data["profiles"] = {}
    elif case == "bad_enum":
        p["scenario"] = "LIVE"
    elif case == "liquidity_missing":
        del p["liquidity"]["max_volume_participation"]
    else:
        data["API_SECRET"] = "not-allowed"
    with pytest.raises(ValueError):
        profiles_from_object(data)


def test_json_duplicate_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version": 1, "schema_version": 1, "profiles": []}')
    with pytest.raises(ValueError, match="duplicada"):
        load_profiles(path)


@pytest.mark.parametrize(
    "scenario,hypothesis",
    [
        (Scenario.FAVORABLE, SequenceHypothesis.TP_FIRST_BOUND),
        (Scenario.BASE, SequenceHypothesis.NONE),
        (Scenario.ADVERSE, SequenceHypothesis.DCA_FIRST_BOUND),
    ],
)
def test_three_intraminute_policies_are_hypotheses_without_fill_authorization(
    profiles: ExecutionProfiles,
    scenario: Scenario,
    hypothesis: SequenceHypothesis,
) -> None:
    assessment = assess_ambiguity(
        profiles.select(scenario).intraminute, dca_candidate=True, tp_candidate=True
    )
    assert assessment.ambiguous and assessment.hypothesis is hypothesis
    assert not assessment.fill_authorized and not assessment.sequence_observed
    single = assess_ambiguity(
        profiles.select(scenario).intraminute, dca_candidate=True, tp_candidate=False
    )
    assert not single.ambiguous and single.hypothesis is SequenceHypothesis.NONE
    assert not single.fill_authorized


@pytest.mark.parametrize(
    "quantity,within", [("0.01", True), ("0.05", True), ("0.06", False), ("1000", False)]
)
def test_liquidity_cap_is_not_a_fill_or_price_depth(
    candles: tuple[Candle, ...], profiles: ExecutionProfiles, quantity: str, within: bool
) -> None:
    candle = candles[0]
    result = assess_liquidity(
        candle,
        Quantity(Decimal(quantity)),
        profiles.select(Scenario.BASE).liquidity,
        at=candle.close_time,
    )
    assert result.traded_base_volume == Decimal("1") and result.modeled_volume_cap == Decimal(
        "0.05"
    )
    assert result.symbol.value == "1000SHIBUSDC" and result.within_volume_cap is within
    assert result.queue_unknown and result.price_depth_unknown and not result.fill_authorized


def test_liquidity_rejects_future_volume_and_zero_volume_has_no_capacity(
    candles: tuple[Candle, ...], profiles: ExecutionProfiles
) -> None:
    candle = candles[0]
    policy = profiles.select(Scenario.BASE).liquidity
    with pytest.raises(ValueError, match="aún no disponible"):
        assess_liquidity(
            candle, Quantity(Decimal("1")), policy, at=candle.close_time - timedelta(microseconds=1)
        )
    zero = replace(candle, volume=Decimal("0"), taker_buy_base_volume=Decimal("0"))
    result = assess_liquidity(zero, Quantity(Decimal("0.01")), policy, at=zero.close_time)
    assert (
        result.modeled_volume_cap == 0
        and not result.within_volume_cap
        and not result.fill_authorized
    )


def test_liquidity_context_is_independent_and_contract_cannot_authorize_fills(
    candles: tuple[Candle, ...], profiles: ExecutionProfiles
) -> None:
    candle = replace(candles[0], volume=Decimal("1.23456789123456789123456789"))
    policy = profiles.select(Scenario.BASE).liquidity
    requested = Quantity(Decimal("0.1"))
    expected = assess_liquidity(candle, requested, policy, at=candle.close_time)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        assert assess_liquidity(candle, requested, policy, at=candle.close_time) == expected
        assert ctx.prec == 2
    with pytest.raises(ValueError):
        replace(expected, fill_authorized=True)
    ambiguous = assess_ambiguity(
        IntraminutePolicy.FAVORABLE_BOUND, dca_candidate=True, tp_candidate=True
    )
    with pytest.raises(ValueError):
        replace(ambiguous, fill_authorized=True)


@pytest.mark.parametrize("value", ["not-a-decimal", "NaN", "Infinity", "-1"])
def test_malformed_decimal_config_is_rejected_without_context_dependency(value: str) -> None:
    from decimal import InvalidOperation

    data = json.loads(FIXTURE_PATH.read_text())
    data["profiles"][1]["spread_bps"] = value
    with localcontext() as ctx:
        ctx.traps[InvalidOperation] = False
        with pytest.raises(ValueError):
            profiles_from_object(data)
