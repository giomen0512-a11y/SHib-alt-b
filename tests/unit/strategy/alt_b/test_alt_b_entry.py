from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Context, Decimal, Inexact, Rounded, localcontext
from itertools import product
from typing import cast

import pytest

import trading_engine.strategy.alt_b.entry as entry_module
from trading_engine.models.market import Candle
from trading_engine.models.values import Price, Symbol
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.entry import EntryEvaluation, evaluate_entry_checkpoint

CONFIG = AltBConfig()


def evaluate(history: tuple[Candle, ...], checkpoint: datetime) -> EntryEvaluation:
    return evaluate_entry_checkpoint(history, checkpoint, CONFIG)


def close_at(history: tuple[Candle, ...], index: int, value: str) -> tuple[Candle, ...]:
    candles = list(history)
    candle = candles[index]
    price = Price(Decimal(value))
    candles[index] = replace(
        candle,
        close=price,
        high=Price(max(candle.high.value, price.value)),
        low=Price(min(candle.low.value, price.value)),
    )
    return tuple(candles)


def current_volume(
    history: tuple[Candle, ...], volume: str, ratio: str = "0.5"
) -> tuple[Candle, ...]:
    with localcontext(Context(prec=80)):
        amount = Decimal(volume)
        taker = amount * Decimal(ratio)
    return (
        *history[:-15],
        *(replace(c, volume=amount, taker_buy_base_volume=taker) for c in history[-15:]),
    )


def mock_rv(monkeypatch: pytest.MonkeyPatch, checkpoint: datetime, current: str = "2") -> None:
    def snapshot(candles: tuple[Candle, ...], at: datetime) -> Decimal:
        assert len(candles) == 31 and candles[-1].close_time == at
        return Decimal(current) if at == checkpoint else Decimal("1")

    monkeypatch.setattr(entry_module, "rv30m_snapshot", snapshot)


@pytest.fixture(scope="module")
def valid_evaluation(history: tuple[Candle, ...], checkpoint: datetime) -> EntryEvaluation:
    return evaluate(history, checkpoint)


def test_full_26h_all_filters_pass_and_no_veto(valid_evaluation: EntryEvaluation) -> None:
    result = valid_evaluation
    assert result.data_valid and result.entry_allowed and result.reason == "entry_allowed"
    assert result.change_24h_pass and result.range_4h_pass and result.pullback_1h_pass
    assert result.veto_a is False and result.veto_b is False
    assert result.buy_ratio_real == Decimal("0.5")
    assert result.v15 == result.med_vol_4h == result.med_vol_24h == Decimal("15")
    assert result.rv30m is not None and result.rv_ref_24h is not None
    assert result.rv30m > 0 and result.rv_ref_24h > 0


def test_full_native_veto_a_blocks_entry(history: tuple[Candle, ...], checkpoint: datetime) -> None:
    candles = current_volume(close_at(history, -1, "99"), "8")
    result = evaluate(candles, checkpoint)
    assert result.data_valid and not result.entry_allowed
    assert result.change_24h_pass and result.range_4h_pass and result.pullback_1h_pass
    assert result.veto_a is True and result.veto_b is False and result.reason == "veto_a"


def test_full_native_veto_b_blocks_entry(history: tuple[Candle, ...], checkpoint: datetime) -> None:
    candles = (
        *history[:-31],
        *(
            replace(
                c,
                high=Price(Decimal("108")),
                low=Price(Decimal("94")),
                close=Price(Decimal("95") if i % 2 == 0 else Decimal("105")),
            )
            for i, c in enumerate(history[-31:], start=1)
        ),
    )
    result = evaluate(current_volume(candles, "2.5", "0.25"), checkpoint)
    assert result.data_valid and not result.entry_allowed
    assert result.change_24h_pass and result.range_4h_pass and result.pullback_1h_pass
    assert result.veto_a is False and result.veto_b is True and result.reason == "veto_b"


@pytest.mark.parametrize(
    "current,expected_pass",
    [("95.5", True), ("95.4999", False), ("108", True), ("108.0001", False)],
)
def test_change_24h_inclusive_bounds_and_i_minus_96(
    history: tuple[Candle, ...], checkpoint: datetime, current: str, expected_pass: bool
) -> None:
    candles = close_at(close_at(close_at(history, 119, "100"), 134, "80"), -1, current)
    result = evaluate(candles, checkpoint)
    assert result.data_valid and result.change_24h_pass is expected_pass
    assert result.change_24h == Decimal(current) / Decimal("100") - 1
    assert result.change_24h != Decimal(current) / Decimal("80") - 1
    if not expected_pass:
        assert not result.entry_allowed and result.reason == "filters_failed"


@pytest.mark.parametrize("high,expected_pass", [("102.2", True), ("102.19999", False)])
def test_range_exact_16_blocks_includes_current_excludes_i_minus_16(
    history: tuple[Candle, ...], checkpoint: datetime, high: str, expected_pass: bool
) -> None:
    candles = tuple(
        replace(c, high=Price(Decimal("101")), low=Price(Decimal("100"))) if index >= 1320 else c
        for index, c in enumerate(history)
    )
    candles = (
        *candles[:1319],
        replace(candles[1319], high=Price(Decimal("1000")), low=Price(Decimal("1"))),
        *candles[1320:-1],
        replace(candles[-1], high=Price(Decimal(high))),
    )
    result = evaluate(candles, checkpoint)
    assert result.data_valid and result.range_4h_pass is expected_pass
    assert result.range_4h == (Decimal(high) - 100) / 100


@pytest.mark.parametrize("close,expected_pass", [("989", True), ("989.001", False)])
def test_pullback_exact_four_blocks_includes_current_excludes_i_minus_4(
    history: tuple[Candle, ...], checkpoint: datetime, close: str, expected_pass: bool
) -> None:
    candles = tuple(
        replace(c, high=Price(Decimal("999"))) if index >= 1500 else c
        for index, c in enumerate(history)
    )
    candles = (
        *candles[:1499],
        replace(candles[1499], high=Price(Decimal("100000"))),
        *candles[1500:-1],
        replace(candles[-1], high=Price(Decimal("1000")), close=Price(Decimal(close))),
    )
    result = evaluate(candles, checkpoint)
    assert result.data_valid and result.pullback_1h_pass is expected_pass
    assert result.pullback_1h == 1 - Decimal(close) / Decimal("1000")


@pytest.mark.parametrize(
    "volume,close,expected",
    [("8", "99", True), ("8", "99.0001", False), ("7.9999", "99", False), ("1", "100", False)],
)
def test_veto_a_two_conditions_inclusive_and_previous_close(
    history: tuple[Candle, ...], checkpoint: datetime, volume: str, close: str, expected: bool
) -> None:
    candles = close_at(close_at(close_at(history, -61, "100"), -46, "50"), -1, close)
    result = evaluate(current_volume(candles, volume), checkpoint)
    assert result.data_valid and result.veto_a is expected
    assert result.drop_1h == 1 - Decimal(close) / 100
    assert result.med_vol_4h == Decimal("15")


def test_med_vol_4h_exact_16_previous_even_median_excludes_current(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    candles = list(history)
    for level in range(1, 17):
        start = 1290 + level * 15  # i-16..i-1 = bloques 87..102
        candles[start : start + 15] = [
            replace(c, volume=Decimal(level)) for c in candles[start : start + 15]
        ]
    candles[1290:1305] = [replace(c, volume=Decimal("1000")) for c in candles[1290:1305]]
    result = evaluate(current_volume(tuple(candles), "2000"), checkpoint)
    assert result.data_valid and result.med_vol_4h == Decimal("127.5")


def test_med_vol_24h_exact_96_previous_even_median_excludes_current(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    candles = list(history)
    for level in range(1, 97):
        start = 90 + level * 15  # i-96..i-1 = bloques 7..102
        candles[start : start + 15] = [
            replace(c, volume=Decimal(level)) for c in candles[start : start + 15]
        ]
    candles[90:105] = [replace(c, volume=Decimal("10000")) for c in candles[90:105]]
    result = evaluate(current_volume(tuple(candles), "20000"), checkpoint)
    assert result.data_valid and result.med_vol_24h == Decimal("727.5")


@pytest.mark.parametrize(
    "ratio,volume,rv,expected",
    [
        ("0", "2.5", "1.5", True),
        ("0.25", "2.5", "1.5", True),
        ("0.250001", "2.5", "1.5", False),
        ("0.25", "2.49999", "1.5", False),
        ("0.25", "2.5", "1.49999", False),
    ],
)
def test_veto_b_exact_inclusive_boundaries(
    history: tuple[Candle, ...],
    checkpoint: datetime,
    monkeypatch: pytest.MonkeyPatch,
    ratio: str,
    volume: str,
    rv: str,
    expected: bool,
) -> None:
    mock_rv(monkeypatch, checkpoint, rv)
    result = evaluate(current_volume(history, volume, ratio), checkpoint)
    assert result.data_valid and result.buy_ratio_real == Decimal(ratio)
    assert result.veto_b is expected


@pytest.mark.parametrize("buy,volume,rv", list(product([False, True], repeat=3)))
def test_veto_b_requires_all_three_conditions(
    history: tuple[Candle, ...],
    checkpoint: datetime,
    monkeypatch: pytest.MonkeyPatch,
    buy: bool,
    volume: bool,
    rv: bool,
) -> None:
    mock_rv(monkeypatch, checkpoint, "2" if rv else "1")
    result = evaluate(
        current_volume(history, "3" if volume else "1", "0.1" if buy else "0.5"), checkpoint
    )
    assert result.data_valid and result.veto_b is (buy and volume and rv)


def test_rv_reference_exact_96_previous_snapshots_at_15m_excludes_current(
    history: tuple[Candle, ...], checkpoint: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[datetime] = []

    def snapshot(candles: tuple[Candle, ...], at: datetime) -> Decimal:
        assert len(candles) == 31 and candles[-1].close_time == at
        calls.append(at)
        return Decimal(len(calls))

    monkeypatch.setattr(entry_module, "rv30m_snapshot", snapshot)
    result = evaluate(history, checkpoint)
    assert calls == [checkpoint - timedelta(minutes=15 * offset) for offset in range(96, -1, -1)]
    assert result.rv_ref_24h == Decimal("48.5") and result.rv30m == Decimal("97")


def test_native_rv_reference_matches_independent_100_digit_calculation(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    result = evaluate(history, checkpoint)
    with localcontext(Context(prec=100)):
        references: list[Decimal] = []
        for end in range(120, 1560, 15):
            prices = tuple(c.close.value for c in history[end - 31 : end])
            returns = tuple(prices[i].ln() - prices[i - 1].ln() for i in range(1, 31))
            mean = sum(returns, Decimal("0")) / 30
            references.append((sum(((r - mean) ** 2 for r in returns), Decimal("0")) / 30).sqrt())
        assert len(references) == 96
        ordered = sorted(references)
        expected = (ordered[47] + ordered[48]) / 2
        assert result.rv_ref_24h is not None
        assert abs(result.rv_ref_24h - expected) < Decimal("1e-48")


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("empty", "insufficient_history"),
        ("short", "insufficient_history"),
        ("gap", "missing_or_nonconsecutive_minute"),
        ("duplicate", "missing_or_nonconsecutive_minute"),
        ("unsorted", "missing_or_nonconsecutive_minute"),
        ("misaligned", "missing_or_nonconsecutive_minute"),
        ("5m", "missing_or_nonconsecutive_minute"),
        ("last", "checkpoint_not_available"),
        ("taker", "missing_current_taker_buy"),
        ("volume", "zero_current_volume"),
        ("med4", "zero_med_vol_4h"),
        ("med24", "zero_med_vol_24h"),
        ("rv_ref", "zero_rv_ref_24h"),
    ],
)
def test_market_data_defects_fail_closed_with_stable_reasons(
    history: tuple[Candle, ...], checkpoint: datetime, defect: str, reason: str
) -> None:
    candles = list(history)
    if defect == "empty":
        candles = []
    elif defect == "short":
        candles = candles[1:]
    elif defect == "gap":
        del candles[300]
    elif defect == "duplicate":
        candles[300] = candles[299]
    elif defect == "unsorted":
        candles[300], candles[301] = candles[301], candles[300]
    elif defect == "misaligned":
        candles[300] = replace(
            candles[300], open_time=candles[300].open_time + timedelta(seconds=1)
        )
    elif defect == "5m":
        candles[300] = replace(
            candles[300], close_time=candles[300].open_time + timedelta(minutes=5)
        )
    elif defect == "last":
        candles = candles[:-1]
    elif defect == "taker":
        candles[-7] = replace(candles[-7], taker_buy_base_volume=None)
    elif defect == "volume":
        candles[-15:] = [
            replace(c, volume=Decimal("0"), taker_buy_base_volume=Decimal("0"))
            for c in candles[-15:]
        ]
    elif defect == "med4":
        candles[-255:-15] = [
            replace(c, volume=Decimal("0"), taker_buy_base_volume=None) for c in candles[-255:-15]
        ]
    elif defect == "med24":
        candles[-1455:-255] = [
            replace(c, volume=Decimal("0"), taker_buy_base_volume=None) for c in candles[-1455:-255]
        ]
    else:
        candles = [replace(c, close=Price(Decimal("100"))) for c in candles]
    result = evaluate(tuple(candles), checkpoint)
    assert result.data_valid is False and result.entry_allowed is False and result.reason == reason
    assert result.veto_a is None and result.veto_b is None


def test_unavailable_required_rv_snapshot_fails_closed(
    history: tuple[Candle, ...], checkpoint: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(candles: tuple[Candle, ...], at: datetime) -> None:
        return None

    monkeypatch.setattr(entry_module, "rv30m_snapshot", unavailable)
    result = evaluate(history, checkpoint)
    assert (
        not result.data_valid
        and not result.entry_allowed
        and result.reason == "rv_snapshot_unavailable"
    )


def test_only_current_block_needs_taker_buy(
    history: tuple[Candle, ...], checkpoint: datetime, valid_evaluation: EntryEvaluation
) -> None:
    candles = (
        *tuple(replace(c, taker_buy_base_volume=None) for c in history[:-15]),
        *history[-15:],
    )
    assert evaluate(candles, checkpoint) == valid_evaluation


@pytest.mark.parametrize("position", [-15, -8, -1])
def test_any_missing_current_taker_buy_blocks_entry(
    history: tuple[Candle, ...], checkpoint: datetime, position: int
) -> None:
    candles = list(history)
    candles[position] = replace(candles[position], taker_buy_base_volume=None)
    result = evaluate(tuple(candles), checkpoint)
    assert not result.data_valid and result.reason == "missing_current_taker_buy"


def test_old_history_outside_required_window_does_not_change_result(
    history: tuple[Candle, ...], checkpoint: datetime, valid_evaluation: EntryEvaluation
) -> None:
    first = history[0]
    old = replace(
        first,
        open_time=first.open_time - timedelta(days=1),
        close_time=first.open_time - timedelta(hours=23),
        volume=Decimal("99999"),
        taker_buy_base_volume=None,
        symbol=Symbol("OLD"),
    )
    assert evaluate((old, old, *history), checkpoint) == valid_evaluation


def test_no_lookahead_future_rejected_and_prefix_unchanged(
    history: tuple[Candle, ...], checkpoint: datetime, valid_evaluation: EntryEvaluation
) -> None:
    future = replace(
        history[-1], open_time=checkpoint, close_time=checkpoint + timedelta(minutes=1)
    )
    for volume in (Decimal("1"), Decimal("10000")):
        revised = replace(future, volume=volume, taker_buy_base_volume=None)
        with pytest.raises(ValueError, match="futuras"):
            evaluate((*history, revised), checkpoint)
        assert evaluate(history, checkpoint) == valid_evaluation
    with pytest.raises(ValueError):
        evaluate((*history[:300], *history[301:], future), checkpoint)


@pytest.mark.parametrize(
    "checkpoint_change", [timedelta(minutes=1), timedelta(seconds=1), timedelta(microseconds=1)]
)
def test_checkpoint_requires_exact_quarter_hour(
    history: tuple[Candle, ...], checkpoint: datetime, checkpoint_change: timedelta
) -> None:
    with pytest.raises(ValueError):
        evaluate(history, checkpoint + checkpoint_change)


def test_checkpoint_rejects_naive_and_non_utc(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    for invalid in (
        checkpoint.replace(tzinfo=None),
        checkpoint.replace(tzinfo=timezone(timedelta(hours=1))),
    ):
        with pytest.raises(ValueError):
            evaluate(history, invalid)


@pytest.mark.parametrize("candles", [[], (None,), "candles"])
def test_evaluation_rejects_wrong_candle_inputs(candles: object, checkpoint: datetime) -> None:
    with pytest.raises(TypeError):
        evaluate(cast(tuple[Candle, ...], candles), checkpoint)


def test_evaluation_rejects_wrong_checkpoint_config_or_configured_symbol(
    history: tuple[Candle, ...], checkpoint: datetime
) -> None:
    with pytest.raises(TypeError):
        evaluate(history, cast(datetime, "time"))
    with pytest.raises(TypeError):
        evaluate_entry_checkpoint(history, checkpoint, cast(AltBConfig, None))
    with pytest.raises(ValueError):
        evaluate_entry_checkpoint(history, checkpoint, AltBConfig(symbol=Symbol("OTHER")))
    other = tuple(replace(c, symbol=Symbol("OTHER")) for c in history)
    assert evaluate_entry_checkpoint(
        other, checkpoint, AltBConfig(symbol=Symbol("OTHER"))
    ) == evaluate(history, checkpoint)


def test_one_hundred_runs_are_identical_and_do_not_mutate_input(
    history: tuple[Candle, ...], checkpoint: datetime, valid_evaluation: EntryEvaluation
) -> None:
    original = tuple((c.close, c.volume, c.taker_buy_base_volume) for c in history)
    for _ in range(100):
        assert evaluate(history, checkpoint) == valid_evaluation
    assert tuple((c.close, c.volume, c.taker_buy_base_volume) for c in history) == original


@pytest.mark.parametrize("precision", [2, 6, 28, 80])
def test_evaluation_independent_of_context_and_preserves_it(
    history: tuple[Candle, ...],
    checkpoint: datetime,
    valid_evaluation: EntryEvaluation,
    precision: int,
) -> None:
    with localcontext() as ambient:
        ambient.prec = precision
        ambient.rounding = ROUND_DOWN
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        ambient.Emin = -9
        ambient.Emax = 9
        ambient.clear_flags()
        before = ambient.copy()
        assert evaluate(history, checkpoint) == valid_evaluation
        assert ambient.prec == before.prec and ambient.rounding == before.rounding
        assert ambient.traps == before.traps and ambient.flags == before.flags
        assert ambient.Emin == before.Emin and ambient.Emax == before.Emax


def test_entry_evaluation_is_frozen(valid_evaluation: EntryEvaluation) -> None:
    for field in fields(valid_evaluation):
        with pytest.raises(FrozenInstanceError):
            setattr(valid_evaluation, field.name, getattr(valid_evaluation, field.name))


@pytest.mark.parametrize(
    "field",
    [
        "data_valid",
        "entry_allowed",
        "change_24h_pass",
        "range_4h_pass",
        "pullback_1h_pass",
        "veto_a",
        "veto_b",
    ],
)
@pytest.mark.parametrize("value", [1, "True", None])
def test_entry_evaluation_strict_boolean_fields(
    valid_evaluation: EntryEvaluation, field: str, value: object
) -> None:
    with pytest.raises(TypeError):
        replace(valid_evaluation, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    [
        "change_24h",
        "range_4h",
        "pullback_1h",
        "v15",
        "med_vol_4h",
        "drop_1h",
        "buy_ratio_real",
        "med_vol_24h",
        "rv30m",
        "rv_ref_24h",
    ],
)
@pytest.mark.parametrize("value", [1, True, "1", Decimal("NaN"), Decimal("Infinity"), None])
def test_entry_evaluation_strict_finite_decimal_metrics(
    valid_evaluation: EntryEvaluation, field: str, value: object
) -> None:
    with pytest.raises(ValueError if isinstance(value, Decimal) else TypeError):
        replace(valid_evaluation, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_allowed": False},
        {"reason": ""},
        {"reason": "unknown"},
        {"v15": Decimal("0")},
        {"med_vol_4h": Decimal("0")},
        {"med_vol_24h": Decimal("0")},
        {"rv_ref_24h": Decimal("0")},
        {"change_24h": Decimal("-1")},
        {"range_4h": Decimal("-1")},
        {"drop_1h": Decimal("1")},
        {"buy_ratio_real": Decimal("1.1")},
        {"pullback_1h": Decimal("1")},
    ],
)
def test_entry_evaluation_rejects_incoherent_values(
    valid_evaluation: EntryEvaluation, changes: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        replace(valid_evaluation, **changes)  # type: ignore[arg-type]


def test_invalid_evaluation_never_allows_entry(checkpoint: datetime) -> None:
    result = EntryEvaluation(checkpoint, False, False, "insufficient_history")
    assert result.change_24h is None and result.veto_a is None
    with pytest.raises(ValueError):
        replace(result, entry_allowed=True)
    with pytest.raises(ValueError):
        replace(result, reason="entry_allowed")
