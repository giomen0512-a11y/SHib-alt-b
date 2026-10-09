"""Replay causal: sin ejecución económica ni política de Quotes históricos."""

import ast
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from trading_engine.models.account import Position
from trading_engine.models.decisions import StrategyResult, StrategyState
from trading_engine.models.enums import OrderStatus, Side
from trading_engine.models.ids import FillId, OrderId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.values import Price, Quantity, Symbol
from trading_engine.orchestration.models import CoordinatorState
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source
from trading_engine.replay.models import (
    EntryControlEvent,
    FillEvent,
    Gap,
    ReplayEvent,
    ReplayState,
    event_key,
)
from trading_engine.replay.runner import ReplayRunner
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.engine import AltBEngine
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import SYMBOL, T


def quote(seconds: int) -> Quote:
    return Quote(
        SYMBOL, T + timedelta(seconds=seconds), Price(Decimal("99")), Price(Decimal("100"))
    )


def fixture_order(
    intent: OrderIntent,
    number: int,
    seconds: int,
    status: OrderStatus = OrderStatus.OPEN,
    quantity: Decimal = Decimal("0"),
) -> Order:
    return Order(
        OrderId(UUID(int=number)),
        intent,
        status,
        quantity,
        intent.created_at,
        T + timedelta(seconds=seconds),
    )


def fixture_fill(order: Order, number: int, price: str = "100") -> FillEvent:
    return FillEvent(
        Fill(
            FillId(UUID(int=number)),
            order.order_id,
            order.updated_at,
            Price(Decimal(price)),
            Quantity(order.filled_quantity),
        ),
        order,
    )


def base_event(signaled: ReplayState) -> FillEvent:
    intent = ReplayRunner().run((quote(120),), signaled).outputs.order_intents[0]
    return fixture_fill(
        fixture_order(intent, 100, 121, OrderStatus.FILLED, intent.quantity.value), 1000
    )


def round_events(signaled: ReplayState) -> tuple[ReplayEvent, ...]:
    runner = ReplayRunner()
    entry = base_event(signaled)
    state = runner.run((quote(120), entry), signaled)
    management = state.strategy_state.round_state
    assert management is not None
    tp = fixture_order(management.take_profits[0].intent, 200, 122)
    dca = fixture_fill(
        fixture_order(
            management.dcas[0].intent, 301, 123, OrderStatus.PARTIALLY_FILLED, Decimal("0.25")
        ),
        2001,
        "98",
    )
    return quote(120), entry, tp, dca


def store(path: Path, candles: tuple[Candle, ...]) -> None:
    with FuturesSQLite(path) as db:
        db.import_candles(
            candles,
            symbol=SYMBOL,
            source=Source.REST,
            file="synthetic-fixture",
            period="offline",
            as_of=candles[-1].close_time if candles else T,
        )


def test_chronological_closed_candles_and_signal_at_15m(
    signaled: ReplayState, history: tuple[Candle, ...]
) -> None:
    assert signaled.strategy_state.market.candles_1m == history
    assert signaled.audit.candles_processed == 1560
    assert signaled.audit.checkpoints_evaluated == 104
    assert len(signaled.outputs.signals) == len(signaled.outputs.scheduled_actions) == 1
    assert signaled.outputs.signals[0].timestamp == T
    assert signaled.outputs.scheduled_actions[0].not_before == T + timedelta(seconds=120)
    assert not signaled.outputs.order_intents


def test_exact_delay_and_no_base_without_admissible_quote(signaled: ReplayState) -> None:
    runner = ReplayRunner()
    early = runner.run((quote(119),), signaled)
    assert not early.outputs.order_intents and not early.outputs.evaluated_actions
    assert not early.due_actions
    due = runner.run((EntryControlEvent(T + timedelta(seconds=120), True),), early)
    assert (
        len(due.due_actions) == 1
        and due.coordinator.pending_actions == signaled.coordinator.pending_actions
    )
    assert due.strategy_state.pending_entry is not None and not due.outputs.order_intents
    evaluated = runner.run((quote(120),), due)
    assert len(evaluated.outputs.order_intents) == len(evaluated.outputs.evaluated_actions) == 1
    assert evaluated.outputs.order_intents[0].created_at == T + timedelta(seconds=120)
    assert not evaluated.coordinator.pending_actions and not evaluated.due_actions


def test_later_quote_evaluates_at_its_own_time_without_backdating(signaled: ReplayState) -> None:
    evaluated = ReplayRunner().run((quote(180),), signaled)
    assert evaluated.outputs.evaluated_actions[0].quote == quote(180)
    assert evaluated.outputs.order_intents[0].created_at == T + timedelta(seconds=180)
    assert signaled.outputs.scheduled_actions[0].not_before == T + timedelta(seconds=120)


def test_retry_candle_does_not_repeat_signal_or_action(
    signaled: ReplayState, history: tuple[Candle, ...]
) -> None:
    assert ReplayRunner().run((history[-1],), signaled) == signaled


def test_quote_retry_does_not_duplicate_base(signaled: ReplayState) -> None:
    runner = ReplayRunner()
    evaluated = runner.run((quote(120),), signaled)
    assert runner.run((quote(120),), evaluated) == evaluated
    assert len(runner.run((quote(121),), evaluated).outputs.order_intents) == 1


def test_future_quote_cannot_resolve_a_past_event(signaled: ReplayState) -> None:
    future = ReplayRunner().run((quote(180),), signaled)
    with pytest.raises(ValueError, match="orden causal"):
        ReplayRunner().run((quote(120),), future)
    assert future.through == T + timedelta(seconds=180)
    assert signaled.strategy_state.base is None


def test_equal_timestamp_priority_control_before_candle_before_quote(
    history: tuple[Candle, ...],
) -> None:
    runner = ReplayRunner()
    prefix = runner.run((EntryControlEvent(history[0].open_time, True), *history[:-1]))
    paused = runner.run((EntryControlEvent(T, False), history[-1], quote(0)), prefix)
    assert not paused.outputs.signals and not paused.outputs.scheduled_actions
    with pytest.raises(ValueError, match="orden causal"):
        runner.run((quote(0), EntryControlEvent(T, False)), prefix)
    signaled = runner.run((history[-1], quote(0)), prefix)
    assert len(signaled.outputs.signals) == 1 and not signaled.outputs.order_intents


def test_explicit_order_fill_priorities_and_effective_fact_time(signaled: ReplayState) -> None:
    runner = ReplayRunner()
    entry = base_event(signaled)
    assert event_key(entry.order)[1] == 1 and event_key(entry)[1] == 2
    result = runner.run((quote(120), entry.order, entry), signaled)
    assert len(result.outputs.order_intents) == 9
    assert result.outputs.observed_orders == (entry.order,)
    assert result.outputs.observed_fills == (entry.fill,)
    assert result.strategy_state.round_state is not None
    assert result.strategy_state.round_state.p0 == entry.fill.price
    assert runner.run((entry,), result) == result
    delayed_report = replace(entry.order, updated_at=T + timedelta(seconds=130))
    delayed = FillEvent(entry.fill, delayed_report)
    assert event_key(delayed)[0] == delayed_report.updated_at
    observed = runner.run((quote(120), delayed), signaled)
    assert all(
        i.created_at == delayed_report.updated_at for i in observed.outputs.order_intents[1:]
    )


def test_partial_base_is_not_a_confirmed_round(signaled: ReplayState) -> None:
    runner = ReplayRunner()
    base = runner.run((quote(120),), signaled)
    intent = base.outputs.order_intents[0]
    partial = fixture_fill(
        fixture_order(intent, 100, 121, OrderStatus.PARTIALLY_FILLED, Decimal("0.25")), 1000
    )
    result = runner.run((partial,), base)
    assert result.strategy_state.round_state is None and len(result.outputs.order_intents) == 1
    assert result.strategy_state.base is not None and result.strategy_state.base.p0 is None


def test_cancel_request_never_confirms_a_cancel_or_creates_replacement(
    signaled: ReplayState,
) -> None:
    events = round_events(signaled)
    state = ReplayRunner().run(events, signaled)
    management = state.strategy_state.round_state
    assert management is not None
    assert len(state.outputs.cancel_requests) == 1 and len(state.outputs.order_intents) == 9
    assert len(management.pending_cancellations()) == 1
    assert management.take_profits[0].order is not None
    assert management.take_profits[0].order.status is OrderStatus.OPEN
    assert len(management.take_profits) == 1
    assert state.report()["PENDIENTE"] is not None
    assert ReplayRunner().run((events[-1],), state) == state


def test_tp_replacement_only_from_explicit_terminal_order(signaled: ReplayState) -> None:
    runner = ReplayRunner()
    waiting = runner.run(round_events(signaled), signaled)
    management = waiting.strategy_state.round_state
    assert management is not None and management.take_profits[0].order is not None
    canceled = replace(
        management.take_profits[0].order,
        status=OrderStatus.CANCELED,
        updated_at=T + timedelta(seconds=124),
    )
    result = runner.run((canceled,), waiting)
    assert len(result.outputs.order_intents) == 10
    assert (
        result.outputs.order_intents[-1].side is Side.SELL
        and result.outputs.order_intents[-1].reduce_only
    )
    assert len(result.strategy_state.round_state.take_profits) == 2  # type: ignore[union-attr]


def test_ohlc_crosses_dca_and_tp_without_any_fill_or_spread(
    signaled: ReplayState, history: tuple[Candle, ...]
) -> None:
    runner = ReplayRunner()
    opened = runner.run((quote(120), base_event(signaled)), signaled)
    wide = replace(
        history[-1],
        open_time=T + timedelta(minutes=2),
        close_time=T + timedelta(minutes=3),
        high=Price(Decimal("10000")),
        low=Price(Decimal("1")),
    )
    result = runner.run((wide,), opened)
    assert result.outputs == opened.outputs
    assert result.strategy_state.round_state == opened.strategy_state.round_state
    assert result.strategy_state.base == opened.strategy_state.base
    assert not result.outputs.cancel_requests
    assert len(result.outputs.observed_fills) == 1


def test_gap_resets_warmup_and_cancels_due_entry(
    signaled: ReplayState, history: tuple[Candle, ...]
) -> None:
    next_candle = replace(
        history[-1], open_time=T + timedelta(minutes=2), close_time=T + timedelta(minutes=3)
    )
    result = ReplayRunner().run((next_candle,), signaled)
    assert result.strategy_state.market.candles_1m == (next_candle,)
    assert result.strategy_state.pending_entry is None and not result.coordinator.pending_actions
    assert result.audit.gaps == (Gap(T, T + timedelta(minutes=2)),)
    assert result.audit.minutes_missing == 2
    assert result.outputs == signaled.outputs


def test_pause_cancels_due_action_without_evaluating_old_signal(signaled: ReplayState) -> None:
    result = ReplayRunner().run(
        (EntryControlEvent(T + timedelta(seconds=120), False), quote(120)), signaled
    )
    assert not result.outputs.order_intents and not result.outputs.evaluated_actions
    assert not result.coordinator.pending_actions and not result.due_actions
    resumed = ReplayRunner().run(
        (EntryControlEvent(T + timedelta(seconds=121), True), quote(121)), result
    )
    assert not resumed.outputs.order_intents


@pytest.mark.parametrize("split", [1, 1200, 1560, 1561, 1562, 1563])
def test_full_replay_equals_segments_at_any_event_boundary(
    history: tuple[Candle, ...], signaled: ReplayState, split: int
) -> None:
    runner = ReplayRunner()
    tape: tuple[ReplayEvent, ...] = (
        EntryControlEvent(history[0].open_time, True),
        *history,
        *round_events(signaled),
    )
    full = runner.run(tape)
    first = runner.run(tape[:split])
    restored = replace(
        first,
        coordinator=replace(first.coordinator),
        outputs=replace(first.outputs),
        audit=replace(first.audit),
    )
    segmented = runner.run(tape[split:], restored)
    assert segmented == full
    assert segmented.outputs == full.outputs and segmented.coordinator == full.coordinator
    assert segmented.strategy_state.round_history == full.strategy_state.round_history
    assert (
        full.strategy_state.round_state is not None
        and full.strategy_state.round_state.closed_at is None
    )


def test_golden_base_and_signal_ids_remain_identical(signaled: ReplayState) -> None:
    result = ReplayRunner().run((quote(120),), signaled)
    assert str(result.outputs.signals[0].signal_id) == "562f6059-cb33-5ccc-b208-68a643d24e07"
    assert (
        str(result.outputs.scheduled_actions[0].action_id) == "ec7ccf71-85e9-5cc3-b976-fa85b7dd9648"
    )
    assert str(result.outputs.order_intents[0].intent_id) == "3e8dc55a-bdbd-527b-84df-0b01fdc28862"


def test_explicit_close_preserves_round_history_across_segments(signaled: ReplayState) -> None:
    runner = ReplayRunner()
    entry = base_event(signaled)
    opened = runner.run((quote(120), entry), signaled)
    management = opened.strategy_state.round_state
    assert management is not None
    tp = management.take_profits[0].intent
    exit_event = fixture_fill(
        fixture_order(tp, 200, 122, OrderStatus.FILLED, tp.quantity.value), 4000, "101"
    )
    cancels = tuple(
        fixture_order(leg.intent, 301 + i, 123, OrderStatus.CANCELED)
        for i, leg in enumerate(management.dcas)
    )
    closing = runner.run((exit_event,), opened)
    assert (
        closing.strategy_state.round_state is not None
        and closing.strategy_state.round_state.closed_at is None
    )
    assert len(closing.outputs.cancel_requests) == 7
    closed = runner.run(cancels, closing)
    full = runner.run((quote(120), entry, exit_event, *cancels), signaled)
    assert closed == full
    assert closed.strategy_state.base is None and closed.strategy_state.round_state is None
    assert len(closed.strategy_state.round_history) == 1
    assert closed.strategy_state.round_history[0].round_state.can_close(
        closed.strategy_state.round_history[0].base
    )


def test_stream_is_consumed_incrementally_using_same_engine(
    history: tuple[Candle, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[Candle] = []
    original = AltBEngine.on_candle

    def inspected(
        self: AltBEngine,
        candle: Candle,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        observed.append(candle)
        return original(self, candle, position, round_state, strategy_state=strategy_state)

    monkeypatch.setattr(AltBEngine, "on_candle", inspected)

    def stream() -> Iterator[ReplayEvent]:
        yield history[0]
        assert observed == [history[0]]
        yield history[1]

    result = ReplayRunner().run(stream())
    assert observed == list(history[:2])
    assert result.strategy_state.market.candles_1m == history[:2]


def test_sqlite_exact_read_only_ingestion(tmp_path: Path, history: tuple[Candle, ...]) -> None:
    path = tmp_path / "market.sqlite3"
    first = replace(
        history[0],
        volume=Decimal("1.00000000000000000000000000001"),
        taker_buy_base_volume=Decimal("0.12345678901234567890123456789"),
    )
    candles = (first, *history[1:3])
    store(path, (*candles, history[3]))  # La fila futura existe pero no se entrega.
    original = path.read_bytes()
    result = ReplayRunner().run_sqlite(path, candles[0].open_time, candles[-1].close_time)
    assert result.strategy_state.market.candles_1m == candles
    assert result.audit.candles_processed == 3 and result.audit.minutes_missing == 0
    assert path.read_bytes() == original
    assert not result.outputs.observed_orders and not result.outputs.observed_fills


def test_sqlite_gaps_initial_internal_trailing_and_empty(
    tmp_path: Path, history: tuple[Candle, ...]
) -> None:
    path = tmp_path / "market.sqlite3"
    store(path, (history[1], history[4]))
    result = ReplayRunner().run_sqlite(path, history[0].open_time, history[6].open_time)
    assert result.audit.minutes_missing == 4
    assert result.audit.gaps == (
        Gap(history[0].open_time, history[1].open_time),
        Gap(history[2].open_time, history[4].open_time),
        Gap(history[5].open_time, history[6].open_time),
    )
    assert result.strategy_state.market.candles_1m == (history[4],)
    empty = ReplayRunner().run_sqlite(path, history[6].open_time, history[8].open_time)
    assert empty.audit.minutes_missing == 2 and empty.audit.candles_processed == 0
    assert not empty.outputs.signals


def test_sqlite_full_equals_contiguous_segments_with_quote_and_pending_gap(
    tmp_path: Path, history: tuple[Candle, ...]
) -> None:
    path = tmp_path / "market.sqlite3"
    store(path, history)
    runner = ReplayRunner()
    start, end = history[0].open_time, T + timedelta(minutes=3)
    control = EntryControlEvent(start, True)
    full = runner.run_sqlite(path, start, end, events=(control, quote(120)))
    prefix = runner.run_sqlite(path, start, T, events=(control,))
    segmented = runner.run_sqlite(path, T, end, events=(quote(120),), state=prefix)
    assert full == segmented
    assert len(full.outputs.order_intents) == 1
    assert not full.outputs.observed_fills
    assert full.strategy_state.base is not None and full.strategy_state.round_state is None
    assert full.audit.gaps == (Gap(T, end),)


def test_sqlite_due_action_stays_pending_at_range_end_without_quote(
    tmp_path: Path, history: tuple[Candle, ...]
) -> None:
    path = tmp_path / "market.sqlite3"
    store(path, history)
    state = ReplayRunner().run_sqlite(
        path,
        history[0].open_time,
        T + timedelta(minutes=2),
        events=(EntryControlEvent(history[0].open_time, True),),
    )
    assert len(state.due_actions) == 1 and len(state.coordinator.pending_actions) == 1
    assert not state.outputs.order_intents
    assert state.strategy_state.last_event_at == T
    assert state.through == T + timedelta(minutes=2)


def test_sqlite_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite3"
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        ReplayRunner().run_sqlite(path, T, T + timedelta(minutes=1))
    assert not path.exists()


def test_sqlite_segments_split_inside_gap_preserve_exact_state(
    tmp_path: Path, history: tuple[Candle, ...]
) -> None:
    path = tmp_path / "market.sqlite3"
    store(path, (*history[:3], *history[6:9]))
    runner = ReplayRunner()
    start, boundary, end = history[0].open_time, history[4].open_time, history[9].open_time
    full = runner.run_sqlite(path, start, end)
    prefix = runner.run_sqlite(path, start, boundary)
    segmented = runner.run_sqlite(path, boundary, end, state=prefix)
    assert full == segmented
    assert full.audit.gaps == (Gap(history[3].open_time, history[6].open_time),)
    assert full.strategy_state.market.candles_1m == history[6:9]
    assert full.audit.candles_processed == 6


def test_checkpoint_cannot_claim_future_audit_evidence(signaled: ReplayState) -> None:
    with pytest.raises(ValueError, match="Evidencia Replay posterior"):
        replace(signaled, audit=replace(signaled.audit, range_end=T + timedelta(seconds=1)))
    with pytest.raises(ValueError, match="watermark"):
        replace(signaled, last_key=(T - timedelta(seconds=1), 3))


def test_quote_for_other_symbol_is_rejected_without_consuming_action(signaled: ReplayState) -> None:
    with pytest.raises(ValueError, match="otro símbolo"):
        ReplayRunner().run((replace(quote(120), symbol=Symbol("1000SHIBUSDT")),), signaled)
    assert len(signaled.coordinator.pending_actions) == 1 and not signaled.outputs.order_intents


@pytest.mark.parametrize(
    "problem", ["future_quote", "candle_fixture", "noncontiguous", "unaligned"]
)
def test_sqlite_rejects_invalid_ranges_or_fixture_events(
    tmp_path: Path, history: tuple[Candle, ...], problem: str
) -> None:
    path = tmp_path / "market.sqlite3"
    store(path, history[:3])
    runner = ReplayRunner()
    start, end = history[0].open_time, history[3].open_time
    if problem == "future_quote":
        with pytest.raises(ValueError, match="fuera del rango"):
            runner.run_sqlite(path, start, end, events=(quote(120),))
    elif problem == "candle_fixture":
        with pytest.raises(ValueError, match="solo de SQLite"):
            runner.run_sqlite(path, start, end, events=(history[0],))
    elif problem == "unaligned":
        with pytest.raises(ValueError, match="alineado"):
            runner.run_sqlite(path, start + timedelta(seconds=1), end)
    else:
        first = runner.run_sqlite(path, start, history[1].open_time)
        with pytest.raises(ValueError, match="contiguos"):
            runner.run_sqlite(path, history[2].open_time, end, state=first)


def test_audit_evidence_categories_and_no_profit_claim(signaled: ReplayState) -> None:
    report = ReplayRunner().run(round_events(signaled), signaled).report()
    assert report["symbol"] == "1000SHIBUSDC" and report["market"] == "BINANCE_USDM_FUTURES"
    observed = cast(dict[str, int], report["OBSERVADO"])
    simulated = cast(dict[str, int], report["SIMULADO"])
    assert observed == {"candles": 1560, "orders": 3, "fills": 2}
    assert simulated["rounds_opened"] == 1 and simulated["rounds_closed"] == 0
    assert simulated["cancel_requests"] == 1
    assert "profitability" in cast(tuple[str, ...], report["NO VERIFICABLE"])
    final = report["final_state"]
    assert isinstance(final, CoordinatorState)
    assert isinstance(final.strategy_state, AltBStrategyState)


def test_runner_models_are_frozen_and_reject_bad_identity_or_time(signaled: ReplayState) -> None:
    with pytest.raises(FrozenInstanceError):
        signaled.through = T  # type: ignore[misc]
    with pytest.raises(ValueError, match="posterior al cursor"):
        replace(signaled, through=T - timedelta(seconds=1), last_key=None)
    with pytest.raises(ValueError, match="otra configuración"):
        ReplayRunner(AltBConfig(order_budget=Decimal("2400"))).run((), signaled)
    with pytest.raises(ValueError, match="1000SHIBUSDC"):
        ReplayRunner(AltBConfig(symbol=Symbol("TESTUSDC")))
    with pytest.raises(ValueError):
        EntryControlEvent(T.replace(tzinfo=None), True)


def test_replay_does_not_use_wall_clock_random_ids_or_execution_adapter() -> None:
    package = Path(__file__).parents[3] / "src/trading_engine/replay"
    assert package.is_dir()
    for path in package.resolve().glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not isinstance(node.func, ast.Attribute) or node.func.attr not in {
                    "now",
                    "utcnow",
                    "uuid4",
                    "submit",
                    "poll",
                    "import_candles",
                    "note",
                }
