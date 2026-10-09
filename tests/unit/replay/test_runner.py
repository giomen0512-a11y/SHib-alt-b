"""Contrato causal, solo lectura, coordinación única y reanudación integral."""

import ast
import json
import sqlite3
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from trading_engine.binance.klines import LAUNCH, minute_ms
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import OrderIntent
from trading_engine.models.signals import Signal
from trading_engine.models.values import Price, Symbol
from trading_engine.replay.clock import SimulatedClock
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import ReplayRequest, ReplaySnapshot
from trading_engine.replay.ports import (
    EntryControl,
    FillEvidence,
    FixtureFacts,
    FixtureQuotes,
    QuoteEvidence,
    ReplayFact,
)
from trading_engine.replay.runner import ReplayRunner
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.state import AltBStrategyState

from .conftest import DUE, END, START, SYMBOL, T, write_db


def state_of(snapshot: ReplaySnapshot) -> AltBStrategyState:
    state = snapshot.coordinator.strategy_state
    assert isinstance(state, AltBStrategyState)
    return state


def intents(snapshot: ReplaySnapshot) -> tuple[OrderIntent, ...]:
    return tuple(
        e.payload
        for e in snapshot.emissions
        if e.kind == "order_intent" and isinstance(e.payload, OrderIntent)
    )


def test_sqlite_read_only_and_exact_decimal(database: Path, candles: tuple[Candle, ...]) -> None:
    before = sha256(database.read_bytes()).hexdigest()
    with SQLiteHistory(database) as history:
        assert (
            tuple(history.read(SYMBOL, candles[0].open_time, candles[3].open_time)) == candles[:3]
        )
        assert history.store.connection.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            history.store.connection.execute("DELETE FROM futures_klines_1m")
    assert sha256(database.read_bytes()).hexdigest() == before


def test_warmup_checkpoint_signal_and_no_execution(
    database: Path, replay_request: ReplayRequest
) -> None:
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request)
        report = runner.run(T)
        s = runner.snapshot
        assert report.interruption is None
        assert report.stats.warmup_applied == report.stats.warmup_read == 1560
        assert report.stats.evaluated_read == 15 and report.stats.checkpoints == 1
        assert report.stats.effective_start == START
        assert report.signals == report.scheduled_actions == 1
        signal = next(e.payload for e in s.emissions if e.kind == "signal")
        assert isinstance(signal, Signal) and signal.timestamp == T
        assert s.coordinator.pending_actions[0].not_before == DUE
        assert not intents(s) and state_of(s).base is None
        assert len(state_of(s).market.candles_1m) == 1560
        assert all(c.close_time <= T for c in state_of(s).market.candles_1m)


def test_warmup_has_no_signals_or_intents(database: Path, replay_request: ReplayRequest) -> None:
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request)
        report = runner.run(START)
        assert report.signals == report.order_intents == report.stats.checkpoints == 0
        assert report.stats.evaluated_read == 0 and report.stats.warmup_applied == 1560
        assert state_of(runner.snapshot).allow_entries


def test_exact_t_plus_120_and_no_future_candle(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes
) -> None:
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes)
        before = runner.run(DUE - timedelta(microseconds=1))
        assert before.order_intents == 0 and len(runner.snapshot.coordinator.pending_actions) == 1
        report = runner.run(DUE)
        assert report.fixture_intents == report.order_intents == 1
        base = state_of(runner.snapshot).base
        assert base is not None and base.intent.created_at == DUE
        assert base.reference_quote == quotes.quotes[0]
        assert base.order is None and base.fills == ()
        assert report.cancelled_actions == 0
        assert not runner.snapshot.coordinator.pending_actions
        assert all(c.close_time <= DUE for c in state_of(runner.snapshot).market.candles_1m)


def test_missing_quote_invalidates_forever_and_reports_reason(
    database: Path, replay_request: ReplayRequest
) -> None:
    late = FixtureQuotes(
        (Quote(SYMBOL, DUE + timedelta(seconds=1), Price(Decimal("99")), Price(Decimal("100"))),)
    )
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request, quotes=late)
        report = runner.run()
        assert report.actions_without_quote == report.cancelled_actions == 1
        assert report.order_intents == 0
        assert state_of(runner.snapshot).pending_entry is None
        assert not runner.snapshot.coordinator.pending_actions
        assert any(
            e.kind == "missing_quote" and e.at == DUE and e.provenance == "missing_quote"
            for e in runner.snapshot.emissions
        )
        previous = runner.snapshot
        assert runner.run() == report and runner.snapshot == previous


@dataclass(frozen=True)
class BadQuotes:
    quote: Quote
    identity: str = "bad-quotes-fixture"

    def at(self, symbol: Symbol, at: datetime) -> QuoteEvidence:
        return QuoteEvidence(self.quote, "SYNTHETIC_FIXTURE")


@pytest.mark.parametrize("case", ["old", "future", "symbol"])
def test_invalid_quotes_never_emit_base(
    database: Path, replay_request: ReplayRequest, case: str
) -> None:
    at = DUE + (
        timedelta(seconds=-1)
        if case == "old"
        else timedelta(seconds=1)
        if case == "future"
        else timedelta()
    )
    q = Quote(
        Symbol("BTCUSDC") if case == "symbol" else SYMBOL,
        at,
        Price(Decimal("99")),
        Price(Decimal("100")),
    )
    with SQLiteHistory(database) as history:
        report = ReplayRunner(history, replay_request, quotes=BadQuotes(q)).run()
        assert report.actions_without_quote == 1 and report.order_intents == 0


@pytest.mark.parametrize("pause_at", [DUE - timedelta(microseconds=1), DUE])
def test_pause_precedes_due_at_equal_timestamp(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes, pause_at: datetime
) -> None:
    facts = FixtureFacts((ReplayFact(pause_at, EntryControl(False), 0),))
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        report = runner.run()
        assert report.order_intents == report.actions_without_quote == 0
        assert report.cancelled_actions == 1
        assert not state_of(runner.snapshot).allow_entries
        assert state_of(runner.snapshot).pending_entry is None


def test_default_entries_disabled(database: Path) -> None:
    with SQLiteHistory(database) as history:
        report = ReplayRunner(history, ReplayRequest(START, END)).run()
        assert report.signals == report.order_intents == 0
        assert report.block_reason == "entries_paused"


def test_gap_at_due_cancels_before_quote_and_requires_new_warmup(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
) -> None:
    data = tuple(c for c in candles if c.close_time != DUE)
    path = write_db(tmp_path / "gap.sqlite3", data)
    with SQLiteHistory(path) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes)
        report = runner.run()
        assert report.cancelled_actions == 1 and report.order_intents == 0
        assert report.actions_without_quote == 0
        assert len(state_of(runner.snapshot).market.candles_1m) == 3
        assert report.stats.invalid_periods[-1].start == DUE - timedelta(minutes=1)
        assert report.stats.invalid_periods[-1].end == DUE


def test_1559_minutes_fail_closed(tmp_path: Path, candles: tuple[Candle, ...]) -> None:
    selected = tuple(c for c in candles if T - timedelta(minutes=1559) <= c.open_time < T)
    path = write_db(tmp_path / "short.sqlite3", selected)
    with SQLiteHistory(path) as history:
        replay_request = ReplayRequest(T - timedelta(minutes=1), T, allow_entries=True)
        runner = ReplayRunner(history, replay_request)
        report = runner.run()
        assert report.stats.minutes_read == 1559 and report.signals == 0
        assert len(state_of(runner.snapshot).market.candles_1m) == 1559
        assert report.block_reason == "insufficient_history"


def test_explicit_fill_starts_dca_tp_and_duplicate_is_idempotent(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes, facts: FixtureFacts
) -> None:
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        report = runner.run()
        assert report.interruption is None
        assert report.order_intents == report.fixture_intents == 9
        state = state_of(runner.snapshot)
        assert state.base is not None and len(state.base.fills) == 1
        assert state.round_state is not None and len(state.round_state.dcas) == 7
        assert len(state.round_state.take_profits) == 1
        assert state.round_state.p0 == Price(Decimal("100"))
        assert len({i.intent_id for i in intents(runner.snapshot)}) == 9


@pytest.mark.parametrize(
    "cut",
    [
        START - timedelta(minutes=5),
        START,
        T,
        DUE - timedelta(microseconds=1),
        DUE,
        DUE + timedelta(seconds=1),
        DUE + timedelta(seconds=1, microseconds=500000),
    ],
)
def test_resume_equals_continuous_with_full_evidence(
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    cut: datetime,
) -> None:
    with SQLiteHistory(database) as history:
        full = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        expected = full.run()
        partial = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        partial.run(cut)
        saved = partial.snapshot
        resumed = ReplayRunner(history, replay_request, quotes=quotes, facts=facts, snapshot=saved)
        assert resumed.run() == expected
        assert resumed.snapshot == full.snapshot
        assert saved.clock.now == cut  # snapshot anterior permanece inmutable


def test_many_fragments_and_retries_keep_fingerprint(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes
) -> None:
    with SQLiteHistory(database) as history:
        full = ReplayRunner(history, replay_request, quotes=quotes)
        expected = full.run()
        fragmented = ReplayRunner(history, replay_request, quotes=quotes)
        for at in (START, START + timedelta(minutes=7), T, DUE, END):
            fragmented.run(at)
            before = fragmented.snapshot
            fragmented.run(at)
            assert fragmented.snapshot == before
        assert fragmented.report() == expected


def test_snapshots_are_frozen_and_binding_rejects_changed_sources(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes
) -> None:
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes)
        runner.run(T)
        saved = runner.snapshot
        with pytest.raises(FrozenInstanceError):
            saved.fact_cursor = 5  # type: ignore[misc]
        with pytest.raises(ValueError, match="misma sesión"):
            ReplayRunner(history, replay_request, snapshot=saved)
        with pytest.raises(ValueError, match="misma sesión"):
            ReplayRunner(
                history,
                replay_request,
                config=AltBConfig(order_budget=Decimal("1000")),
                quotes=quotes,
                snapshot=saved,
            )
        incomplete = replace(saved, coordinator=replace(saved.coordinator, pending_actions=()))
        with pytest.raises(ValueError, match="Integridad"):
            ReplayRunner(history, replay_request, quotes=quotes, snapshot=incomplete)
        with SQLiteHistory(database) as other:
            with pytest.raises(ValueError, match="misma sesión"):
                ReplayRunner(other, replay_request, quotes=quotes, snapshot=saved)


def test_future_corrupt_data_does_not_affect_earlier_decision(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
) -> None:
    path = write_db(tmp_path / "future.sqlite3", candles)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE futures_klines_1m SET high='1' WHERE open_time_ms=?", (minute_ms(DUE),))
    with SQLiteHistory(path) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes)
        before = runner.run(DUE)
        assert before.order_intents == 1 and before.interruption is None
        after = runner.run()
        assert after.interruption is not None and after.processed_until == DUE + timedelta(
            minutes=1
        )
        assert after.order_intents == 1


@pytest.mark.parametrize("conflict", [False, True])
def test_malformed_sqlite_duplicate_or_conflict_blocks(
    tmp_path: Path, candles: tuple[Candle, ...], replay_request: ReplayRequest, conflict: bool
) -> None:
    path = write_db(tmp_path / "duplicates.sqlite3", candles)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE corrupt AS SELECT * FROM futures_klines_1m")
        db.execute(
            "INSERT INTO corrupt SELECT * FROM futures_klines_1m WHERE open_time_ms=?",
            (minute_ms(T - timedelta(minutes=1)),),
        )
        if conflict:
            db.execute(
                "UPDATE corrupt SET close='102' WHERE rowid=(SELECT max(rowid) FROM corrupt)"
            )
        db.execute("DROP TABLE futures_klines_1m")
        db.execute("ALTER TABLE corrupt RENAME TO futures_klines_1m")
    with SQLiteHistory(path) as history:
        report = ReplayRunner(history, replay_request).run()
        assert report.interruption is not None
        assert ("conflicting" if conflict else "duplicate") in report.interruption
        assert report.signals == report.order_intents == 0


def test_manifest_conflict_is_explicit_fail_closed(
    tmp_path: Path, candles: tuple[Candle, ...], replay_request: ReplayRequest
) -> None:
    path = write_db(tmp_path / "manifest.sqlite3", candles)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE import_manifest SET state='failed', error='Conflicto 1000SHIBUSDC/123: fixture'"
        )
    with SQLiteHistory(path) as history:
        report = ReplayRunner(history, replay_request).run()
        assert report.interruption is not None and report.interruption.startswith(
            "sqlite_conflicts:"
        )
        assert report.stats.minutes_read == report.order_intents == 0


def test_pre_launch_and_empty_database_never_fabricate(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    path = write_db(tmp_path / "later.sqlite3", candles[:1])
    with SQLiteHistory(path) as history:
        report = ReplayRunner(
            history,
            ReplayRequest(LAUNCH - timedelta(minutes=1), LAUNCH + timedelta(minutes=1), True),
        ).run()
        assert report.stats.minutes_read == report.signals == 0
        assert report.stats.effective_start is None
        assert {p.reason for p in report.stats.invalid_periods} == {"pre_contract", "gap"}


def test_report_contains_no_profit_and_is_json_serializable(
    database: Path, replay_request: ReplayRequest
) -> None:
    with SQLiteHistory(database) as history:
        report = ReplayRunner(history, replay_request).run()
        serialized = json.dumps(report.as_dict(), sort_keys=True)
        assert "FINANCIAL_RESULTS_NOT_AVAILABLE" in serialized
        assert "profit" not in report.as_dict() and "pnl" not in report.as_dict()
        assert len(report.event_digest) == 64 and report.event_count > 1580
        assert report.market == "BINANCE_USDM_FUTURES" and report.interval == "1m"


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        T + timedelta(seconds=1),
    ],
)
def test_request_rejects_non_utc_or_non_minute(bad: datetime) -> None:
    with pytest.raises(ValueError):
        ReplayRequest(bad, END)


def test_clock_never_goes_back_and_until_never_reads_past_end(
    database: Path, replay_request: ReplayRequest
) -> None:
    clock = SimulatedClock(T)
    assert clock.advance(DUE).now == DUE and clock.now == T
    with pytest.raises(ValueError):
        clock.advance(T - timedelta(seconds=1))
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request)
        runner.run(T)
        with pytest.raises(ValueError):
            runner.run(T - timedelta(seconds=1))
        with pytest.raises(ValueError):
            runner.run(END + timedelta(seconds=1))


def test_future_order_or_fill_rejected_at_port(base_fact: ReplayFact) -> None:
    assert isinstance(base_fact.payload, FillEvidence)
    with pytest.raises(ValueError, match="futuro"):
        replace(base_fact, received_at=DUE)
    with pytest.raises(ValueError, match="futuro"):
        ReplayFact(DUE, base_fact.payload.order, 0)


def test_no_clock_sleep_exchange_execution_or_strategy_copy() -> None:
    root = Path(__file__).resolve().parents[3] / "src/trading_engine/replay"
    forbidden_imports = {"time", "socket", "urllib", "requests", "http", "subprocess"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] in forbidden_imports for a in node.names)
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module.split(".")[0] not in forbidden_imports
                assert module not in {
                    "trading_engine.binance.public_data",
                    "trading_engine.execution.contracts",
                    "trading_engine.strategy.alt_b.math",
                    "trading_engine.strategy.alt_b.entry",
                }
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"now", "sleep", "submit", "poll", "import_candles"}


def test_ohlc_touching_dca_and_tp_keeps_execution_unknown(
    tmp_path: Path,
    candles: tuple[Candle, ...],
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    base_fact: ReplayFact,
) -> None:
    touched = tuple(
        replace(c, high=Price(Decimal("110")), low=Price(Decimal("80")))
        if c.close_time == DUE + timedelta(minutes=1)
        else c
        for c in candles
    )
    path = write_db(tmp_path / "both.sqlite3", touched)
    with SQLiteHistory(path) as history:
        runner = ReplayRunner(
            history, replay_request, quotes=quotes, facts=FixtureFacts((base_fact,))
        )
        runner.run(DUE + timedelta(seconds=1))
        before = state_of(runner.snapshot)
        report = runner.run()
        after = state_of(runner.snapshot)
        assert after.base == before.base and after.round_state == before.round_state
        assert report.order_intents == 9 and report.cancel_requests == 0
        assert after.round_state is not None
        assert all(not leg.fills and leg.order is None for leg in after.round_state.legs)


def test_tp_cancel_replace_close_and_resume_pending_cancellation(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    from trading_engine.models.enums import OrderStatus
    from trading_engine.orchestration.coordinator import StrategyCoordinator
    from trading_engine.strategy.alt_b.events.engine import AltBEngine

    from .conftest import fill_for, order_for

    with SQLiteHistory(database) as history:
        seed = ReplayRunner(
            history, replay_request, quotes=quotes, facts=FixtureFacts((base_fact,))
        )
        seed.run(DUE + timedelta(seconds=1))
        reference = seed.snapshot.coordinator
        state = state_of(seed.snapshot)
        management = state.round_state
        assert management is not None
        coordinator = StrategyCoordinator(AltBEngine())
        dca = order_for(
            management.dcas[0].intent,
            301,
            DUE + timedelta(seconds=3),
            OrderStatus.PARTIALLY_FILLED,
            Decimal("0.25"),
        )
        dca_fact = ReplayFact(dca.updated_at, FillEvidence(fill_for(dca, 2001, "98"), dca), 1)
        changed = coordinator.on_fill(reference, dca_fact.payload.fill, dca)  # type: ignore[union-attr]
        assert len(changed.cancel_requests) == 1
        # Confirmar cancelación exige un hecho Order explícito.
        tp = order_for(
            management.take_profits[0].intent, 200, DUE + timedelta(seconds=4), OrderStatus.CANCELED
        )
        replacement = coordinator.on_order(changed.state, tp)
        assert len(replacement.order_intents) == 1
        final_tp = replacement.order_intents[0]
        tp_fill_order = order_for(
            final_tp, 201, DUE + timedelta(seconds=5), OrderStatus.FILLED, final_tp.quantity.value
        )
        tp_fill_fact = ReplayFact(
            tp_fill_order.updated_at,
            FillEvidence(fill_for(tp_fill_order, 5000, "101"), tp_fill_order),
            3,
        )
        closing = coordinator.on_fill(replacement.state, tp_fill_fact.payload.fill, tp_fill_order)  # type: ignore[union-attr]
        assert len(closing.cancel_requests) == 7
        closing_state = closing.state.strategy_state
        assert (
            isinstance(closing_state, AltBStrategyState) and closing_state.round_state is not None
        )
        cancellations = tuple(
            ReplayFact(
                DUE + timedelta(seconds=6),
                order_for(
                    leg.intent,
                    301 + i,
                    DUE + timedelta(seconds=6),
                    OrderStatus.CANCELED,
                    leg.executed_quantity,
                ),
                4 + i,
            )
            for i, leg in enumerate(closing_state.round_state.dcas)
        )
        facts = FixtureFacts(
            (base_fact, dca_fact, ReplayFact(tp.updated_at, tp, 2), tp_fill_fact, *cancellations)
        )
        continuous = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        expected = continuous.run()
        fragmented = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        pending_report = fragmented.run(DUE + timedelta(seconds=3))
        saved = fragmented.snapshot
        pending = state_of(saved).round_state
        assert pending is not None and len(pending.cancel_requests) == 1
        assert pending.take_profits[0].order is None  # solicitud no equivale a confirmación
        assert pending_report.order_intents == 9 and pending_report.cancel_requests == 1
        resumed = ReplayRunner(history, replay_request, quotes=quotes, facts=facts, snapshot=saved)
        assert resumed.run() == expected and resumed.snapshot == continuous.snapshot
        final = state_of(resumed.snapshot)
        assert final.base is None and final.round_state is None and len(final.round_history) == 1
        assert final.round_history[0].round_state.status(final.round_history[0].base) == "CLOSED"
        assert len(final.round_history[0].base.fills) == 1
        assert expected.order_intents == 10 and expected.cancel_requests == 8


def test_same_timestamp_fact_priority_is_independent_of_input_order(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    assert isinstance(base_fact.payload, FillEvidence)
    # Control false, Order FILLED y Fill observados juntos: PAUSE conserva evidencia de ronda.
    at = base_fact.received_at
    control = ReplayFact(at, EntryControl(False), 2)
    order = ReplayFact(at, base_fact.payload.order, 1)
    sources = [FixtureFacts((base_fact, order, control)), FixtureFacts((control, order, base_fact))]
    assert sources[0].facts[0] == control and sources[0].facts[1] == order
    with SQLiteHistory(database) as history:
        first = ReplayRunner(history, replay_request, quotes=quotes, facts=sources[0])
        second = ReplayRunner(history, replay_request, quotes=quotes, facts=sources[1])
        assert first.run() == second.run()
        assert first.snapshot == second.snapshot
        assert not state_of(first.snapshot).allow_entries
        assert state_of(first.snapshot).base is not None
        assert state_of(first.snapshot).round_state is not None


def test_partial_order_without_fill_blocks_and_late_fill_reconciles(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    assert isinstance(base_fact.payload, FillEvidence)
    at = base_fact.received_at
    order = ReplayFact(at, base_fact.payload.order, 0)
    delayed = replace(base_fact, received_at=at + timedelta(seconds=10), sequence=1)
    facts = FixtureFacts((order, delayed))
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        pending = runner.run(at)
        assert pending.order_intents == 1
        assert state_of(runner.snapshot).base is not None
        assert state_of(runner.snapshot).base.fills == ()  # type: ignore[union-attr]
        assert state_of(runner.snapshot).round_state is None
        resumed = ReplayRunner(
            history, replay_request, quotes=quotes, facts=facts, snapshot=runner.snapshot
        )
        report = resumed.run()
        assert report.interruption is None and report.order_intents == 9
        assert state_of(resumed.snapshot).round_state is not None


def test_conflicting_same_fill_id_interrupts_and_preserves_previous_evidence(
    database: Path, replay_request: ReplayRequest, quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    assert isinstance(base_fact.payload, FillEvidence)
    changed = replace(
        base_fact,
        received_at=DUE + timedelta(seconds=2),
        sequence=1,
        payload=replace(
            base_fact.payload, fill=replace(base_fact.payload.fill, price=Price(Decimal("99")))
        ),
    )
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(
            history, replay_request, quotes=quotes, facts=FixtureFacts((base_fact, changed))
        )
        report = runner.run()
        assert report.interruption is not None
        state = state_of(runner.snapshot)
        assert state.base is not None and state.base.fills == (base_fact.payload.fill,)
        assert report.order_intents == 9


def test_warmup_after_gap_requires_1560_new_minutes(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    gap_close = START + timedelta(minutes=5)
    initial = candles[0].open_time
    data = tuple(
        replace(
            candles[i % 2],
            open_time=initial + timedelta(minutes=i),
            close_time=initial + timedelta(minutes=i + 1),
        )
        for i in range(3150)
        if initial + timedelta(minutes=i + 1) != gap_close
    )
    path = write_db(tmp_path / "new-warmup.sqlite3", data)
    period = ReplayRequest(START, START + timedelta(minutes=1590), True)
    with SQLiteHistory(path) as history:
        runner = ReplayRunner(history, period)
        before = runner.run(START + timedelta(minutes=1565))
        assert before.signals == 0
        assert len(state_of(runner.snapshot).market.candles_1m) == 1560
        after = runner.run(START + timedelta(minutes=1575))
        assert after.signals == 1 and after.order_intents == 0


def test_runner_never_modifies_sqlite_or_connects_network(
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import urllib.request

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Replay no debe hacer red")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    before = sha256(database.read_bytes()).hexdigest()
    with SQLiteHistory(database) as history:
        report = ReplayRunner(history, replay_request, quotes=quotes, facts=facts).run()
        assert report.order_intents == 9 and report.interruption is None
    assert sha256(database.read_bytes()).hexdigest() == before


def test_exact_sqlite_precision_survives_hostile_decimal_context(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    from decimal import localcontext

    exact = replace(
        candles[0],
        volume=Decimal("1.123456789012345678901234567890123456789"),
        taker_buy_base_volume=Decimal("0.123456789012345678901234567890123456789"),
    )
    path = write_db(tmp_path / "precision.sqlite3", (exact,))
    with localcontext() as context:
        context.prec = 2
        with SQLiteHistory(path) as history:
            assert tuple(history.read(SYMBOL, exact.open_time, exact.close_time)) == (exact,)
        assert context.prec == 2


def test_launch_violation_in_existing_sqlite_is_rejected(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    path = write_db(tmp_path / "pre-contract.sqlite3", candles[:1])
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE futures_klines_1m SET open_time_ms=?, close_time_ms_exclusive=?",
            (minute_ms(LAUNCH - timedelta(minutes=1)), minute_ms(LAUNCH)),
        )
    with SQLiteHistory(path) as history:
        report = ReplayRunner(history, ReplayRequest(LAUNCH, LAUNCH + timedelta(minutes=1))).run()
        assert report.interruption is not None and "lanzamiento" in report.interruption
        assert report.stats.minutes_read == 0


def test_end_boundary_does_not_expire_future_action(database: Path, quotes: FixtureQuotes) -> None:
    period = ReplayRequest(START, T, True)
    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, period, quotes=quotes)
        report = runner.run()
        assert report.signals == 1 and report.actions_without_quote == 0
        assert runner.snapshot.coordinator.pending_actions[0].not_before == DUE


def test_warmup_missing_last_minute_is_not_reported_as_complete(
    tmp_path: Path, candles: tuple[Candle, ...], replay_request: ReplayRequest
) -> None:
    path = write_db(
        tmp_path / "warmup-end-gap.sqlite3", tuple(c for c in candles if c.close_time != START)
    )
    with SQLiteHistory(path) as history:
        report = ReplayRunner(history, replay_request).run(START)
        assert report.stats.warmup_applied == 0 and report.stats.warmup_read == 1559


def test_quote_and_fact_fixture_duplicates_are_rejected(
    quotes: FixtureQuotes, base_fact: ReplayFact
) -> None:
    with pytest.raises(ValueError, match="duplicados"):
        FixtureQuotes(quotes.quotes * 2)
    with pytest.raises(ValueError, match="sequence"):
        FixtureFacts((base_fact, base_fact))


@pytest.mark.parametrize("loss", ["evidence", "cursor", "results", "clock"])
def test_incomplete_or_altered_snapshot_is_rejected(
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    loss: str,
) -> None:
    from trading_engine.orchestration.models import CoordinatorState

    with SQLiteHistory(database) as history:
        runner = ReplayRunner(history, replay_request, quotes=quotes, facts=facts)
        runner.run(DUE + timedelta(seconds=2))
        saved = runner.snapshot
        if loss == "evidence":
            state = state_of(saved)
            candles_only = AltBStrategyState(
                market=state.market,
                allow_entries=state.allow_entries,
                last_event_at=state.last_event_at,
            )
            invalid = replace(saved, coordinator=CoordinatorState(strategy_state=candles_only))
        elif loss == "cursor":
            invalid = replace(saved, fact_cursor=0)
        elif loss == "results":
            invalid = replace(saved, emissions=())
        else:
            invalid = replace(saved, clock=SimulatedClock(saved.clock.now + timedelta(seconds=1)))
        with pytest.raises(ValueError, match="Integridad"):
            ReplayRunner(history, replay_request, quotes=quotes, facts=facts, snapshot=invalid)


def test_sqlite_session_retains_consistent_view_when_external_writer_changes_fixture(
    tmp_path: Path, candles: tuple[Candle, ...]
) -> None:
    path = write_db(tmp_path / "stable-view.sqlite3", candles)
    with SQLiteHistory(path) as history:
        with sqlite3.connect(path) as writer:
            writer.execute(
                "UPDATE futures_klines_1m SET close='102' WHERE open_time_ms=?",
                (minute_ms(candles[0].open_time),),
            )
        assert (
            tuple(history.read(SYMBOL, candles[0].open_time, candles[0].close_time)) == candles[:1]
        )
    with SQLiteHistory(path) as changed:
        assert tuple(changed.read(SYMBOL, candles[0].open_time, candles[0].close_time))[
            0
        ].close == Price(Decimal("102"))
