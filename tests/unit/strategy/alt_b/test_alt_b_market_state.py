import ast
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Context, Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from typing import cast

import pytest

import trading_engine.strategy.alt_b.state_market as market_state_module
from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.entry import EntryEvaluation, evaluate_entry_checkpoint
from trading_engine.strategy.alt_b.state_market import (
    AltBMarketState,
    AltBMarketStep,
    MarketIngestResult,
    ingest_closed_1m,
)

CONFIG = AltBConfig()


def run_sequence(candles: tuple[Candle, ...]) -> tuple[AltBMarketStep, ...]:
    state = AltBMarketState()
    steps: list[AltBMarketStep] = []
    for candle in candles:
        step = ingest_closed_1m(state, candle, CONFIG)
        state = step.state
        steps.append(step)
    return tuple(steps)


@pytest.fixture(scope="module")
def sequence_27h(history: tuple[Candle, ...]) -> tuple[Candle, ...]:
    start = history[0].open_time
    with localcontext(Context(prec=80)):
        return tuple(
            replace(
                history[0],
                open_time=start + timedelta(minutes=i),
                close_time=start + timedelta(minutes=i + 1),
                close=Price(Decimal("100") + Decimal(i % 19) / 100),
                volume=Decimal(i % 11 + 1),
                taker_buy_base_volume=Decimal(i % 11 + 1) / 2,
            )
            for i in range(1620)
        )


@pytest.fixture(scope="module")
def sequence_steps(sequence_27h: tuple[Candle, ...]) -> tuple[AltBMarketStep, ...]:
    return run_sequence(sequence_27h)


def test_empty_state_contains_only_immutable_history() -> None:
    state = AltBMarketState()
    assert state.candles_1m == ()
    assert [field.name for field in fields(state)] == ["candles_1m"]
    with pytest.raises(FrozenInstanceError):
        state.candles_1m = ()  # type: ignore[misc]


def test_first_candle_is_accepted_without_mutating_inputs(history: tuple[Candle, ...]) -> None:
    state = AltBMarketState()
    candle = history[0]
    original = replace(candle)
    step = ingest_closed_1m(state, candle, CONFIG)
    assert step.result is MarketIngestResult.ACCEPTED
    assert step.state.candles_1m == (candle,)
    assert step.state.candles_1m[0] is candle
    assert step.evaluation is None
    assert state.candles_1m == () and candle == original
    with pytest.raises(FrozenInstanceError):
        step.result = MarketIngestResult.GAP_RESET  # type: ignore[misc]


@pytest.mark.parametrize("candles", [[], [None], (None,), "history"])
def test_state_requires_tuple_of_candles(candles: object) -> None:
    with pytest.raises(TypeError):
        AltBMarketState(cast(tuple[Candle, ...], candles))


@pytest.mark.parametrize(
    "defect", ["gap", "duplicate", "out_of_order", "symbol", "5m", "seconds", "microseconds"]
)
def test_state_rejects_invalid_history(history: tuple[Candle, ...], defect: str) -> None:
    candles = list(history[:3])
    if defect == "gap":
        candles[1] = history[3]
    elif defect == "duplicate":
        candles[1] = candles[0]
    elif defect == "out_of_order":
        candles.reverse()
    elif defect == "symbol":
        candles[1] = replace(candles[1], symbol=Symbol("OTHER"))
    elif defect == "5m":
        candles[1] = replace(candles[1], close_time=candles[1].open_time + timedelta(minutes=5))
    else:
        shift = timedelta(seconds=1) if defect == "seconds" else timedelta(microseconds=1)
        candles = [
            replace(c, open_time=c.open_time + shift, close_time=c.close_time + shift)
            for c in candles
        ]
    with pytest.raises(ValueError):
        AltBMarketState(tuple(candles))


def test_state_rejects_history_over_configured_bound(sequence_27h: tuple[Candle, ...]) -> None:
    assert len(AltBMarketState(sequence_27h[: CONFIG.warmup_minutes]).candles_1m) == 1560
    with pytest.raises(ValueError):
        AltBMarketState(sequence_27h[: CONFIG.warmup_minutes + 1])


def test_lengths_1559_1560_1561_and_continuous_eviction(
    sequence_27h: tuple[Candle, ...], sequence_steps: tuple[AltBMarketStep, ...]
) -> None:
    assert len(sequence_steps[1558].state.candles_1m) == 1559
    assert sequence_steps[1558].evaluation is None
    assert sequence_steps[1559].state.candles_1m == sequence_27h[:1560]
    assert sequence_steps[1559].evaluation is not None
    assert sequence_steps[1559].evaluation.data_valid
    assert sequence_steps[1560].state.candles_1m == sequence_27h[1:1561]
    for length, step in enumerate(sequence_steps, start=1):
        assert step.result is MarketIngestResult.ACCEPTED
        assert len(step.state.candles_1m) == min(length, CONFIG.warmup_minutes)
        assert step.state.candles_1m[-1] is sequence_27h[length - 1]
    assert sequence_steps[-1].state.candles_1m == sequence_27h[-1560:]


def test_each_15m_checkpoint_equals_direct_causal_evaluation_for_27h(
    sequence_27h: tuple[Candle, ...], sequence_steps: tuple[AltBMarketStep, ...]
) -> None:
    checked = 0
    for length, step in enumerate(sequence_steps, start=1):
        candle = sequence_27h[length - 1]
        if candle.close_time.minute % 15:
            assert step.evaluation is None
        else:
            checked += 1
            assert step.evaluation == evaluate_entry_checkpoint(
                sequence_27h[:length], candle.close_time, CONFIG
            )
            assert step.evaluation is not None
            if length < CONFIG.warmup_minutes:
                assert not step.evaluation.data_valid
                assert not step.evaluation.entry_allowed
                assert step.evaluation.reason == "insufficient_history"
            else:
                assert step.evaluation.data_valid and step.evaluation.entry_allowed
    assert checked == 108


def test_evaluator_called_exactly_once_only_for_accepted_quarter_hour(
    history: tuple[Candle, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[Candle, ...], datetime, AltBConfig]] = []

    def spy(candles: tuple[Candle, ...], at: datetime, config: AltBConfig) -> EntryEvaluation:
        calls.append((candles, at, config))
        return evaluate_entry_checkpoint(candles, at, config)

    monkeypatch.setattr(market_state_module, "evaluate_entry_checkpoint", spy)
    steps = run_sequence(history[:16])
    assert len(calls) == 1
    assert calls[0] == (history[:15], history[14].close_time, CONFIG)
    duplicate = ingest_closed_1m(steps[14].state, history[14], CONFIG)
    assert duplicate.result is MarketIngestResult.DUPLICATE
    assert duplicate.state is steps[14].state
    assert duplicate.evaluation is None and len(calls) == 1
    assert steps[14].evaluation is not None and not steps[14].evaluation.data_valid


@pytest.mark.parametrize("quarter", [0, 15, 30, 45])
def test_first_candle_at_any_checkpoint_returns_fail_closed(
    history: tuple[Candle, ...], quarter: int
) -> None:
    candle = replace(
        history[0],
        open_time=history[0].open_time.replace(minute=quarter) - timedelta(minutes=1),
        close_time=history[0].open_time.replace(minute=quarter),
    )
    step = ingest_closed_1m(AltBMarketState(), candle, CONFIG)
    assert step.evaluation == evaluate_entry_checkpoint((candle,), candle.close_time, CONFIG)
    assert step.evaluation is not None and step.evaluation.reason == "insufficient_history"


@pytest.mark.parametrize(
    "changes",
    [
        {"close": Price(Decimal("100.1"))},
        {"open": Price(Decimal("100.1"))},
        {"high": Price(Decimal("104"))},
        {"low": Price(Decimal("98"))},
        {"volume": Decimal("2")},
        {"taker_buy_base_volume": None},
        {"taker_buy_base_volume": Decimal("0.25")},
    ],
)
def test_same_interval_with_changed_content_is_rejected(
    history: tuple[Candle, ...], changes: dict[str, object]
) -> None:
    state = AltBMarketState(history[:15])
    revised = replace(history[14], **changes)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="contenido diferente"):
        ingest_closed_1m(state, revised, CONFIG)
    assert state.candles_1m == history[:15]


@pytest.mark.parametrize("index", [0, 1, 13])
def test_any_candle_older_than_cursor_is_rejected(history: tuple[Candle, ...], index: int) -> None:
    state = AltBMarketState(history[:15])
    with pytest.raises(ValueError, match="fuera de orden"):
        ingest_closed_1m(state, history[index], CONFIG)
    assert state.candles_1m == history[:15]


@pytest.mark.parametrize("close_offset", [15, 16])
def test_gap_resets_to_single_candle_and_evaluates_only_if_checkpoint(
    history: tuple[Candle, ...], checkpoint: datetime, close_offset: int
) -> None:
    state = AltBMarketState(history)
    candle = replace(
        history[0],
        open_time=checkpoint + timedelta(minutes=close_offset - 1),
        close_time=checkpoint + timedelta(minutes=close_offset),
    )
    step = ingest_closed_1m(state, candle, CONFIG)
    assert step.result is MarketIngestResult.GAP_RESET
    assert step.state.candles_1m == (candle,)
    assert state.candles_1m == history
    if close_offset == 15:
        assert step.evaluation is not None and step.evaluation.reason == "insufficient_history"
    else:
        assert step.evaluation is None


def test_gap_requires_entire_warmup_again(history: tuple[Candle, ...]) -> None:
    state = AltBMarketState(history)
    shifted = tuple(
        replace(
            c,
            open_time=c.open_time + timedelta(days=2),
            close_time=c.close_time + timedelta(days=2),
        )
        for c in history
    )
    step = ingest_closed_1m(state, shifted[0], CONFIG)
    assert step.result is MarketIngestResult.GAP_RESET
    state = step.state
    for length, candle in enumerate(shifted[1:], start=2):
        step = ingest_closed_1m(state, candle, CONFIG)
        state = step.state
        assert len(state.candles_1m) == length
        if step.evaluation is not None and length < 1560:
            assert not step.evaluation.data_valid
            assert step.evaluation.reason == "insufficient_history"
    assert step.evaluation is not None and step.evaluation.data_valid
    assert state.candles_1m == shifted


def test_missing_taker_buy_stays_none_in_state_and_fails_current_checkpoint(
    history: tuple[Candle, ...],
) -> None:
    state = AltBMarketState(history[:-1])
    candle = replace(history[-1], taker_buy_base_volume=None)
    step = ingest_closed_1m(state, candle, CONFIG)
    assert step.state.candles_1m[-1] is candle
    assert step.state.candles_1m[-1].taker_buy_base_volume is None
    assert step.evaluation is not None and step.evaluation.reason == "missing_current_taker_buy"
    assert not step.evaluation.data_valid
    assert state.candles_1m == history[:-1]


@pytest.mark.parametrize("defect", ["symbol", "5m", "seconds", "microseconds", "provider_close"])
def test_ingestion_rejects_wrong_symbol_or_non_normalized_candle(
    history: tuple[Candle, ...], defect: str
) -> None:
    candle = history[0]
    if defect == "symbol":
        candle = replace(candle, symbol=Symbol("OTHER"))
    elif defect == "5m":
        candle = replace(candle, close_time=candle.open_time + timedelta(minutes=5))
    elif defect == "provider_close":
        candle = replace(candle, close_time=candle.close_time - timedelta(milliseconds=1))
    else:
        shift = timedelta(seconds=1) if defect == "seconds" else timedelta(microseconds=1)
        candle = replace(
            candle, open_time=candle.open_time + shift, close_time=candle.close_time + shift
        )
    with pytest.raises(ValueError):
        ingest_closed_1m(AltBMarketState(), candle, CONFIG)


def test_state_and_candle_must_both_match_configured_symbol(history: tuple[Candle, ...]) -> None:
    other = AltBConfig(symbol=Symbol("OTHER"))
    candle = replace(history[1], symbol=other.symbol)
    with pytest.raises(ValueError):
        ingest_closed_1m(AltBMarketState(history[:1]), candle, other)
    step = ingest_closed_1m(AltBMarketState(), candle, other)
    assert step.state.candles_1m == (candle,)


@pytest.mark.parametrize("field", ["state", "candle", "config"])
@pytest.mark.parametrize("value", [None, "invalid", 1, True])
def test_ingestion_requires_real_domain_inputs(
    history: tuple[Candle, ...], field: str, value: object
) -> None:
    args: dict[str, object] = {"state": AltBMarketState(), "candle": history[0], "config": CONFIG}
    args[field] = value
    with pytest.raises(TypeError):
        ingest_closed_1m(**args)  # type: ignore[arg-type]


def test_one_hundred_identical_transitions_are_equal(history: tuple[Candle, ...]) -> None:
    state = AltBMarketState(history[:-1])
    original = state.candles_1m
    expected = ingest_closed_1m(state, history[-1], CONFIG)
    for _ in range(100):
        assert ingest_closed_1m(state, history[-1], CONFIG) == expected
    assert state.candles_1m is original


def test_entire_27h_sequence_repeated_from_same_initial_state_is_identical(
    sequence_27h: tuple[Candle, ...], sequence_steps: tuple[AltBMarketStep, ...]
) -> None:
    assert run_sequence(sequence_27h) == sequence_steps


@pytest.mark.parametrize("precision", [2, 6, 28, 80])
def test_global_decimal_context_does_not_change_transition(
    history: tuple[Candle, ...], precision: int
) -> None:
    state = AltBMarketState(history[:-1])
    expected = ingest_closed_1m(state, history[-1], CONFIG)
    with localcontext() as ambient:
        ambient.prec = precision
        ambient.rounding = ROUND_UP
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        ambient.clear_flags()
        before = ambient.copy()
        assert ingest_closed_1m(state, history[-1], CONFIG) == expected
        assert ambient.prec == before.prec and ambient.rounding == before.rounding
        assert ambient.traps == before.traps and ambient.flags == before.flags


def test_no_lookahead_future_revisions_cannot_change_earlier_steps(
    sequence_27h: tuple[Candle, ...], sequence_steps: tuple[AltBMarketStep, ...]
) -> None:
    causal = sequence_steps[1559]
    state = sequence_steps[1558].state
    assert ingest_closed_1m(state, sequence_27h[1559], CONFIG) == causal
    altered_future = replace(
        sequence_27h[1560],
        close=Price(Decimal("102")),
        volume=Decimal("1000"),
        taker_buy_base_volume=None,
    )
    later = ingest_closed_1m(causal.state, altered_future, CONFIG)
    assert causal.state.candles_1m[-1] is sequence_27h[1559]
    assert causal.evaluation is not None
    assert all(c.close_time <= causal.evaluation.checkpoint_at for c in causal.state.candles_1m)
    assert ingest_closed_1m(state, sequence_27h[1559], CONFIG) == causal
    with pytest.raises(ValueError, match="fuera de orden"):
        ingest_closed_1m(later.state, sequence_27h[1559], CONFIG)


def test_step_rejects_invalid_types_and_inconsistent_output(history: tuple[Candle, ...]) -> None:
    state = AltBMarketState(history[:1])
    checkpoint_state = AltBMarketState(history[:15])
    evaluation = evaluate_entry_checkpoint(history[:15], history[14].close_time, CONFIG)
    with pytest.raises(TypeError):
        AltBMarketStep(cast(AltBMarketState, None), None, MarketIngestResult.ACCEPTED)
    with pytest.raises(TypeError):
        AltBMarketStep(state, None, cast(MarketIngestResult, "accepted"))
    with pytest.raises(TypeError):
        AltBMarketStep(state, cast(EntryEvaluation, "evaluation"), MarketIngestResult.ACCEPTED)
    with pytest.raises(ValueError):
        AltBMarketStep(AltBMarketState(), None, MarketIngestResult.ACCEPTED)
    with pytest.raises(ValueError):
        AltBMarketStep(state, evaluation, MarketIngestResult.ACCEPTED)
    with pytest.raises(ValueError):
        AltBMarketStep(checkpoint_state, None, MarketIngestResult.ACCEPTED)
    with pytest.raises(ValueError):
        AltBMarketStep(checkpoint_state, evaluation, MarketIngestResult.DUPLICATE)
    with pytest.raises(ValueError):
        AltBMarketStep(checkpoint_state, evaluation, MarketIngestResult.GAP_RESET)
    wrong_checkpoint = replace(
        evaluation, checkpoint_at=evaluation.checkpoint_at + timedelta(minutes=15)
    )
    with pytest.raises(ValueError):
        AltBMarketStep(checkpoint_state, wrong_checkpoint, MarketIngestResult.ACCEPTED)


def test_state_market_has_no_clock_ids_io_or_external_implementation_dependencies() -> None:
    path = Path(market_state_module.__file__ or "")
    tree = ast.parse(path.read_text())
    allowed = {
        "dataclasses",
        "enum",
        "trading_engine.models._validation",
        "trading_engine.models.market",
        "trading_engine.strategy.alt_b.config",
        "trading_engine.strategy.alt_b.entry",
        "trading_engine.strategy.alt_b.market",
    }
    forbidden_calls = {
        "now",
        "utcnow",
        "uuid4",
        "uuid5",
        "sleep",
        "open",
        "print",
        "make_signal_id",
        "make_round_id",
        "make_action_id",
        "make_intent_id",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
        elif isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            assert name not in forbidden_calls
    assert "BuyRatioProxy" not in path.read_text()
