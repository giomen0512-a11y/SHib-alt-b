"""Precios modelados causales, fuentes diferenciadas y replay compatible."""

import ast
from collections.abc import Generator
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from pathlib import Path

import pytest

from trading_engine.binance.klines import minute_ms
from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.replay.execution_model.config import load_profiles
from trading_engine.replay.execution_model.policy import ExecutionPolicy, Scenario
from trading_engine.replay.execution_model.quotes import (
    ModeledQuoteEvidence,
    ModeledQuotes,
    PriceOrigin,
    model_closed_candle,
    price_origin,
)
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayRequest
from trading_engine.replay.ports import FixtureQuotes, NoQuotes, QuoteEvidence
from trading_engine.replay.runner import ReplayRunner
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, START, SYMBOL, T, write_db
from .test_execution_policy import FIXTURE_PATH


@pytest.fixture
def policy() -> ExecutionPolicy:
    return load_profiles(FIXTURE_PATH).select(Scenario.BASE)


def test_closed_reference_spread_once_and_slippage_not_in_quote(
    candles: tuple[Candle, ...], policy: ExecutionPolicy
) -> None:
    candle = replace(candles[0], close=Price(Decimal("100")))
    at = candle.close_time + timedelta(milliseconds=250)
    estimate = model_closed_candle(candle, policy, at=at, dataset_identity="synthetic-prices")
    evidence = estimate.evidence
    assert isinstance(evidence, ModeledQuoteEvidence)
    assert evidence.quote.bid.value == Decimal("99.95")
    assert evidence.quote.ask.value == Decimal("100.05")
    assert evidence.quote.bid.value > 0 and evidence.quote.bid.value <= evidence.quote.ask.value
    assert evidence.quote.timestamp == at and evidence.reference_candle == candle
    assert evidence.available_at == at and evidence.expires_at == candle.close_time + timedelta(
        milliseconds=90000
    )
    assert evidence.provenance == "MODELED_FROM_1M"
    changed = model_closed_candle(
        candle,
        replace(policy, slippage_bps=Decimal("30")),
        at=at,
        dataset_identity="synthetic-prices",
    )
    assert changed.evidence is not None and changed.evidence.quote == evidence.quote
    assert changed.evidence.policy_identity != evidence.policy_identity


def test_quote_formula_is_independent_of_hostile_decimal_context(
    candles: tuple[Candle, ...], policy: ExecutionPolicy
) -> None:
    candle = candles[0]
    at = candle.close_time + timedelta(seconds=1)
    expected = model_closed_candle(candle, policy, at=at, dataset_identity="fixture")
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_UP
        ctx.traps[Inexact] = True
        assert model_closed_candle(candle, policy, at=at, dataset_identity="fixture") == expected
        assert ctx.prec == 2 and ctx.rounding == ROUND_UP


@pytest.mark.parametrize(
    "offset,available", [(-1, False), (0, True), (1, True), (89750, True), (89751, False)]
)
def test_exact_latency_and_freshness_boundaries(
    candles: tuple[Candle, ...], policy: ExecutionPolicy, offset: int, available: bool
) -> None:
    candle = candles[0]
    at = candle.close_time + timedelta(milliseconds=250 + offset)
    estimate = model_closed_candle(candle, policy, at=at, dataset_identity="fixture")
    assert (estimate.evidence is not None) is available
    if not available:
        assert estimate.reason in {"quote_latency_not_elapsed", "stale_reference"}


def test_future_candle_never_provides_quote(
    candles: tuple[Candle, ...], policy: ExecutionPolicy
) -> None:
    candle = candles[0]
    result = model_closed_candle(
        candle, policy, at=candle.close_time - timedelta(microseconds=1), dataset_identity="fixture"
    )
    assert result.evidence is None and result.reason == "candle_not_closed"


def test_latest_latency_eligible_close_and_no_reading_future(
    database: Path, policy: ExecutionPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteHistory(database) as history:
        read = history.read
        calls: list[tuple[datetime, datetime]] = []

        def guarded(
            symbol: Symbol, start: datetime, end: datetime
        ) -> Generator[Candle, None, None]:
            assert end <= DUE
            calls.append((start, end))
            yield from read(symbol, start, end)

        monkeypatch.setattr(history, "read", guarded)
        source = ModeledQuotes(history, policy, "fixture")
        evidence = source.at(SYMBOL, DUE)
        assert isinstance(evidence, ModeledQuoteEvidence)
        assert evidence.reference_candle.close_time == DUE - timedelta(minutes=1)
        assert evidence.available_at == DUE - timedelta(minutes=1) + timedelta(milliseconds=250)
        assert evidence.quote.timestamp == DUE
        assert calls == [(DUE - timedelta(minutes=2), DUE)]


def test_price_does_not_anticipate_next_close(database: Path, policy: ExecutionPolicy) -> None:
    immediate = replace(policy, quote_latency_ms=0)
    with SQLiteHistory(database) as history:
        source = ModeledQuotes(history, immediate, "fixture")
        before = source.at(SYMBOL, DUE - timedelta(microseconds=1))
        after = source.at(SYMBOL, DUE)
        assert isinstance(before, ModeledQuoteEvidence) and isinstance(after, ModeledQuoteEvidence)
        assert before.reference_candle.close_time == DUE - timedelta(minutes=1)
        assert after.reference_candle.close_time == DUE
        assert before.quote.ask != after.quote.ask
        saved = before
        source.at(SYMBOL, DUE + timedelta(minutes=1))
        assert before == saved


@pytest.mark.parametrize("removed_close", [DUE - timedelta(minutes=1), DUE])
def test_gap_in_reference_or_latest_minute_prevents_old_quote_reuse(
    tmp_path: Path, candles: tuple[Candle, ...], policy: ExecutionPolicy, removed_close: datetime
) -> None:
    path = write_db(
        tmp_path / "gap.sqlite3", tuple(c for c in candles if c.close_time != removed_close)
    )
    with SQLiteHistory(path) as history:
        estimate = ModeledQuotes(history, policy, "gap-fixture").estimate(SYMBOL, DUE)
        assert estimate.evidence is None and "gap" in estimate.reason


def test_short_freshness_cannot_restamp_stale_reference(
    database: Path, policy: ExecutionPolicy
) -> None:
    short = replace(policy, freshness_limit_ms=500)
    with SQLiteHistory(database) as history:
        estimate = ModeledQuotes(history, short, "fixture").estimate(SYMBOL, DUE)
        assert estimate.evidence is None and estimate.reason == "stale_reference"


def test_invalid_data_produces_absence_and_future_corruption_is_not_read(
    tmp_path: Path, candles: tuple[Candle, ...], policy: ExecutionPolicy
) -> None:
    import sqlite3

    path = write_db(tmp_path / "invalid.sqlite3", candles)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE futures_klines_1m SET high='1' WHERE open_time_ms=?",
            (minute_ms(DUE),),
        )
    with SQLiteHistory(path) as history:
        source = ModeledQuotes(history, policy, "fixture")
        assert source.at(SYMBOL, DUE) is not None  # la vela corrupta todavía no cerró
        assert source.at(SYMBOL, DUE + timedelta(minutes=1)) is None


def test_price_categories_are_distinct(
    candles: tuple[Candle, ...], policy: ExecutionPolicy
) -> None:
    modeled = model_closed_candle(
        candles[0],
        policy,
        at=candles[0].close_time + timedelta(seconds=1),
        dataset_identity="fixture",
    ).evidence
    assert modeled is not None
    observed = QuoteEvidence(modeled.quote, "REAL_OBSERVED")
    fixture = QuoteEvidence(modeled.quote, "SYNTHETIC_FIXTURE")
    assert [price_origin(e) for e in (observed, modeled, fixture, None)] == [
        PriceOrigin.REAL_OBSERVED,
        PriceOrigin.MODELED,
        PriceOrigin.FIXTURE,
        PriceOrigin.UNAVAILABLE,
    ]
    assert NoQuotes().at(SYMBOL, DUE) is None


def test_quote_timestamps_and_metadata_reject_future_or_inconsistent_evidence(
    candles: tuple[Candle, ...], policy: ExecutionPolicy
) -> None:
    candle = candles[0]
    result = model_closed_candle(
        candle, policy, at=candle.close_time + timedelta(seconds=1), dataset_identity="fixture"
    )
    evidence = result.evidence
    assert evidence is not None
    with pytest.raises(ValueError):
        replace(evidence, available_at=evidence.quote.timestamp + timedelta(microseconds=1))
    with pytest.raises(ValueError):
        replace(evidence, expires_at=evidence.quote.timestamp - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        replace(evidence, provenance="REAL_OBSERVED")
    with pytest.raises(ValueError):
        model_closed_candle(candle, policy, at=datetime(2026, 1, 1), dataset_identity="fixture")


@pytest.mark.parametrize("scenario", list(Scenario))
def test_three_profiles_integrate_without_creating_orders_or_fills(
    database: Path, replay_request: ReplayRequest, scenario: Scenario
) -> None:
    selected = load_profiles(FIXTURE_PATH).select(scenario)
    with SQLiteHistory(database) as history:
        source = ModeledQuotes(history, selected, "SYNTHETIC_FIXTURE_MARKET")
        runner = ReplayRunner(history, replay_request, quotes=source)
        report = runner.run()
        assert report.interruption is None
        assert (
            report.signals
            == report.scheduled_actions
            == report.order_intents
            == report.modeled_intents
            == 1
        )
        assert report.fixture_intents == 0 and report.execution_identity == source.identity
        assert len(report.price_evidence) == 1
        evidence = report.price_evidence[0]
        assert isinstance(evidence, ModeledQuoteEvidence) and evidence.scenario is scenario
        state = report.final_state.strategy_state
        assert isinstance(state, AltBStrategyState) and state.base is not None
        assert state.base.order is None and state.base.fills == () and state.round_state is None
        assert report.financial_results == "FINANCIAL_RESULTS_NOT_AVAILABLE"
        assert all(
            e.provenance == "MODELED_FROM_1M"
            for e in runner.snapshot.emissions
            if e.kind == "order_intent"
        )


@pytest.mark.parametrize("cut", [START, T, DUE - timedelta(microseconds=1), DUE])
def test_same_profile_reconstruction_matches_continuous_replay(
    database: Path, replay_request: ReplayRequest, policy: ExecutionPolicy, cut: datetime
) -> None:
    with SQLiteHistory(database) as history:
        source = ModeledQuotes(history, policy, "fixture")
        continuous = ReplayRunner(history, replay_request, quotes=source)
        expected = continuous.run()
        part = ReplayRunner(history, replay_request, quotes=source)
        part.run(cut)
        reconstructed = ModeledQuotes(history, policy, "fixture")
        resumed = ReplayRunner(
            history, replay_request, quotes=reconstructed, snapshot=part.snapshot
        )
        assert resumed.run() == expected and resumed.snapshot == continuous.snapshot


@pytest.mark.parametrize(
    "change", ["scenario", "spread", "slippage", "latency", "freshness", "liquidity", "dataset"]
)
def test_changed_policy_or_dataset_cannot_resume(
    database: Path, replay_request: ReplayRequest, policy: ExecutionPolicy, change: str
) -> None:
    with SQLiteHistory(database) as history:
        source = ModeledQuotes(history, policy, "fixture")
        runner = ReplayRunner(history, replay_request, quotes=source)
        runner.run(T)
        choices = {
            "scenario": load_profiles(FIXTURE_PATH).select(Scenario.ADVERSE),
            "spread": replace(policy, spread_bps=Decimal("11")),
            "slippage": replace(policy, slippage_bps=Decimal("4")),
            "latency": replace(policy, quote_latency_ms=251),
            "freshness": replace(policy, freshness_limit_ms=90001),
            "liquidity": replace(
                policy,
                liquidity=replace(policy.liquidity, max_volume_participation=Decimal("0.04")),
            ),
            "dataset": policy,
        }
        changed = ModeledQuotes(
            history, choices[change], "other-dataset" if change == "dataset" else "fixture"
        )
        assert changed.identity != source.identity
        with pytest.raises(ValueError, match="mismas|misma"):
            ReplayRunner(history, replay_request, quotes=changed, snapshot=runner.snapshot)


def test_modeled_source_must_use_runner_sqlite_session(
    database: Path, replay_request: ReplayRequest, policy: ExecutionPolicy
) -> None:
    with SQLiteHistory(database) as first, SQLiteHistory(database) as other:
        with pytest.raises(ValueError, match="misma sesión SQLite"):
            ReplayRunner(first, replay_request, quotes=ModeledQuotes(other, policy, "fixture"))


def test_identity_is_in_execution_even_before_first_quote(
    database: Path, replay_request: ReplayRequest, policy: ExecutionPolicy
) -> None:
    with SQLiteHistory(database) as history:
        first = ReplayRunner(
            history, replay_request, quotes=ModeledQuotes(history, policy, "fixture")
        )
        second = ReplayRunner(
            history,
            replay_request,
            quotes=ModeledQuotes(history, replace(policy, slippage_bps=Decimal("5")), "fixture"),
        )
        assert first.report().event_digest != second.report().event_digest
        assert first.report().execution_identity != second.report().execution_identity
        assert not first.report().price_evidence and not second.report().price_evidence


def test_legacy_noquotes_and_fixturequotes_preserve_event_fingerprints(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes, base_fact: object
) -> None:
    from trading_engine.replay.ports import FixtureFacts, ReplayFact

    assert isinstance(base_fact, ReplayFact)
    with SQLiteHistory(database) as history:
        no_quotes = ReplayRunner(history, replay_request, quotes=NoQuotes()).run()
        explicit = ReplayRunner(
            history, replay_request, quotes=quotes, facts=FixtureFacts((base_fact,))
        ).run()
        assert (
            no_quotes.event_digest
            == "1de51441efde90d5059802f62aac9fb21926a16be4dd23b1327109f352d96746"
        )
        assert (
            explicit.event_digest
            == "380b59af21a844a4d1c1685b739f91012060c8c4abc92bd1b927af8aaf72bea1"
        )
        assert no_quotes.execution_identity is explicit.execution_identity is None
        assert no_quotes.order_intents == 0 and explicit.fixture_intents == 9


def test_model_has_no_network_clock_random_strategy_or_execution_dependency(
    database: Path, policy: ExecutionPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket
    import urllib.request

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("El modelo de precios no puede hacer red")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    with SQLiteHistory(database) as history:
        assert ModeledQuotes(history, policy, "fixture").at(SYMBOL, DUE) is not None
    root = Path(__file__).resolve().parents[3] / "src/trading_engine/replay/execution_model"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert not any(
                    a.name.split(".")[0]
                    in {"socket", "urllib", "http", "requests", "random", "time"}
                    for a in node.names
                )
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    (
                        "trading_engine.strategy",
                        "trading_engine.execution",
                        "trading_engine.binance.public_data",
                    )
                )
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in {"Order", "Fill", "OrderIntent"}
                elif isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {"submit", "poll", "sleep", "now"}


def test_modeled_identity_canonical_and_profiles_ordered_on_same_reference(
    database: Path, candles: tuple[Candle, ...]
) -> None:
    profiles = load_profiles(FIXTURE_PATH)
    at = candles[0].close_time + timedelta(seconds=1)
    asks = []
    bids = []
    for policy in profiles.profiles:
        estimate = model_closed_candle(candles[0], policy, at=at, dataset_identity="fixture")
        assert estimate.evidence is not None
        asks.append(estimate.evidence.quote.ask.value)
        bids.append(estimate.evidence.quote.bid.value)
    assert asks == sorted(asks) and bids == sorted(bids, reverse=True)
    with SQLiteHistory(database) as history:
        p = profiles.select(Scenario.BASE)
        first = ModeledQuotes(history, p, "fixture")
        equivalent = ModeledQuotes(history, replace(p, spread_bps=Decimal("10.00")), "fixture")
        assert first.identity == equivalent.identity
        assert first.at(SYMBOL, DUE) == equivalent.at(SYMBOL, DUE)


def test_zero_spread_is_explicit_and_remains_modeled(
    candles: tuple[Candle, ...], policy: ExecutionPolicy
) -> None:
    result = model_closed_candle(
        candles[0],
        replace(policy, spread_bps=Decimal("0"), quote_latency_ms=0),
        at=candles[0].close_time,
        dataset_identity="zero-spread-fixture",
    )
    assert result.evidence is not None
    assert result.evidence.quote.bid == result.evidence.quote.ask == candles[0].close
    assert result.evidence.provenance == "MODELED_FROM_1M"


def test_unsupported_symbol_absent_data_and_invalid_timestamp(
    database: Path, policy: ExecutionPolicy
) -> None:
    with SQLiteHistory(database) as history:
        source = ModeledQuotes(history, policy, "fixture")
        assert source.at(Symbol("BTCUSDC"), DUE) is None
        assert source.at(SYMBOL, START - timedelta(days=3)) is None
        with pytest.raises(ValueError):
            source.at(SYMBOL, datetime(2026, 1, 1))


def test_no_reading_lookahead_from_a_broken_candle_port(
    database: Path,
    candles: tuple[Candle, ...],
    policy: ExecutionPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = replace(candles[0], open_time=DUE, close_time=DUE + timedelta(minutes=1))
    with SQLiteHistory(database) as history:

        def broken(symbol: Symbol, start: datetime, end: datetime) -> Generator[Candle, None, None]:
            yield future

        monkeypatch.setattr(history, "read", broken)
        assert ModeledQuotes(history, policy, "fixture").at(SYMBOL, DUE) is None
