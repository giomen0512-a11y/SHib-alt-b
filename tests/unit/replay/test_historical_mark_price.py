"""Mark independiente: fixtures sintéticos, gate financiero, causalidad y confianza."""

import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from pathlib import Path
from typing import Any

import pytest

from trading_engine.binance.klines import LAUNCH, to_ms
from trading_engine.models.enums import Side
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import (
    FinancialEvent,
    Origin,
    Reference,
)
from trading_engine.replay.financial_performance.models import FinancialObservation
from trading_engine.replay.historical_validation.contracts import Contract, DatasetType
from trading_engine.replay.historical_validation.mark_history import PreparedMarkHistory
from trading_engine.replay.historical_validation.mark_source import (
    HistoricalMarkEvent,
    HistoricalMarkPriceSource,
    MarkPublicationPolicy,
)
from trading_engine.replay.historical_validation.query import HistoricalQuery
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.replay_adapter import (
    DEFAULT_ADAPTER_LIMITS,
    AdapterLimits,
)
from trading_engine.replay.historical_validation.segments import Registration, RegistryProvenance
from trading_engine.replay.historical_validation.verification import (
    VerificationMode,
    VerificationState,
)
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.isolated_margin_risk import IsolatedMarginRisk
from trading_engine.replay.isolated_margin_risk.evidence import FixtureRiskSource
from trading_engine.replay.isolated_margin_risk.models import RiskObservation
from trading_engine.replay.models import ReplayRequest
from trading_engine.replay.ports import FixtureFacts, FixtureQuotes, ReplayFact
from trading_engine.replay.runner import ReplayRunner

from .conftest import DUE, END, START, T
from .test_financial_ledger import apply, execution
from .test_financial_performance import AT, complete, policy
from .test_historical_fast_query import registered, session
from .test_historical_registry import spec
from .test_isolated_margin_risk import margin_policy, schedule

MINUTE = timedelta(minutes=1)
MODES = list(VerificationMode)


def publication(**changes: Any) -> MarkPublicationPolicy:
    return replace(
        MarkPublicationPolicy(
            0,
            59999,
            10080,
            "SYNTHETIC_FIXTURE_ONLY",
            "Synthetic close samples; no Binance latency claim",
        ),
        **changes,
    )


def registration(
    folder: Path,
    *,
    start: datetime = LAUNCH,
    count: int = 3,
    missing: tuple[int, ...] = (),
    price: str = "95",
    name: str = "mark",
    provenance: RegistryProvenance = RegistryProvenance.FIXTURE,
    rows: list[object] | None = None,
) -> Registration:
    end = start + count * MINUTE
    if rows is None:
        rows = [
            [
                to_ms(start + i * MINUTE),
                price,
                price,
                price,
                price,
                "0",
                to_ms(start + (i + 1) * MINUTE) - 1,
                "0",
                0,
                "0",
                "0",
                "0",
            ]
            for i in range(count)
            if i not in missing
        ]
    value = spec(folder, name, rows, DatasetType.MARK, start=start, end=end)
    return replace(
        value,
        declaration=replace(value.declaration, acquisition_time=end + timedelta(days=1)),
        validation_time=end + timedelta(days=2),
        provenance=provenance,
    )


@contextmanager
def prepared(
    path: Path,
    ids: tuple[str, ...],
    start: datetime,
    end: datetime,
    mode: VerificationMode,
    *,
    cutoff: datetime | None = None,
    limits: AdapterLimits = DEFAULT_ADAPTER_LIMITS,
) -> Iterator[PreparedMarkHistory]:
    if mode is VerificationMode.SEALED_SEGMENT_SESSION:
        with session(path, ids) as active:
            with PreparedMarkHistory(
                path,
                ids,
                start,
                end,
                cutoff=end if cutoff is None else cutoff,
                mode=mode,
                session=active,
                limits=limits,
            ) as history:
                yield history
    else:
        with PreparedMarkHistory(
            path, ids, start, end, cutoff=end if cutoff is None else cutoff, limits=limits
        ) as history:
            yield history


@pytest.mark.parametrize("mode", MODES)
def test_exact_mark_records_evidence_and_mode(tmp_path: Path, mode: VerificationMode) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    end = reg.declaration.period_end
    with prepared(path, ids, LAUNCH, end, mode) as history:
        rows = tuple(history.read(0, end))
        expected = tuple(HistoricalQuery(path, ids).read_mark(LAUNCH, end, cutoff=end))
        assert tuple(r.event for r in rows) == expected
        assert [r.sequence for r in rows] == [1, 2, 3]
        assert all(r.continuous for r in rows)
        assert history.evidence.reconciliation.coverage_status == "AVAILABLE_FOR_REVIEW"
        assert history.evidence.authentication == "NOT_AUTHENTICATED"
        assert history.evidence.economic_authorization == "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
        assert history.evidence.source_state is (
            VerificationState.FULL_SOURCE_VERIFIED
            if mode is VerificationMode.STRICT_FULL
            else VerificationState.SEGMENT_VERIFIED
        )
        assert history.evidence.original_state is (
            VerificationState.FULL_SOURCE_VERIFIED
            if mode is VerificationMode.STRICT_FULL
            else VerificationState.SOURCE_NOT_REVALIDATED
        )
        assert history.metrics["rows"] == 3 and history.metrics["blocks"] == 1
        assert history._db is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            history._db.execute("DELETE FROM marks")
    assert not history.path.exists()
    with pytest.raises(ValueError, match="NOT_AVAILABLE"):
        tuple(history.read(0, end))


@pytest.mark.parametrize("mode", MODES)
def test_case_c_latency_exact_cutoff_and_cursor(tmp_path: Path, mode: VerificationMode) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    cutoff = LAUNCH + timedelta(seconds=90)
    with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode, cutoff=cutoff) as history:
        source = HistoricalMarkPriceSource(history, publication(latency_ms=250))
        assert tuple(source.read(0, LAUNCH + timedelta(seconds=59))) == ()
        assert tuple(source.read(0, LAUNCH + MINUTE)) == ()
        assert tuple(source.read(0, LAUNCH + MINUTE + timedelta(milliseconds=249))) == ()
        event = tuple(source.read(0, LAUNCH + MINUTE + timedelta(milliseconds=250)))[0]
        assert isinstance(event, HistoricalMarkEvent)
        assert event.payload.effective_at == LAUNCH + MINUTE
        assert event.received_at == LAUNCH + MINUTE + timedelta(milliseconds=250)
        assert event.historical_event.market_effective_at == LAUNCH
        assert event.historical_event.dataset_ids == ids
        assert event.authorization == "SYNTHETIC_FIXTURE_ONLY"
        assert event.payload.reference.provenance is Origin.FIXTURE
        assert tuple(source.read(1, cutoff)) == ()
        with pytest.raises(ValueError, match="CUTOFF"):
            tuple(source.read(0, cutoff + timedelta(microseconds=1)))
        assert history.evidence.reconciliation.requested_end == reg.declaration.period_end
        assert history.evidence.bindings[0].registration.declaration.acquisition_time > cutoff


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("missing", [(1,), (1, 2)])
def test_case_d_gaps_and_recovery(
    tmp_path: Path, mode: VerificationMode, missing: tuple[int, ...]
) -> None:
    reg = registration(tmp_path, count=5, missing=missing)
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode) as history:
        source = HistoricalMarkPriceSource(history, publication())
        events = tuple(source.read(0, reg.declaration.period_end))
        assert len(events) == 5 - len(missing)
        assert history.evidence.reconciliation.gaps
        assert tuple(source.read(1, LAUNCH + 2 * MINUTE)) == ()
        assert events[0].payload.continuous
        assert not events[1].payload.continuous
        assert events[2].payload.continuous
        assert all(e.payload.effective_at == e.received_at for e in events)


@pytest.mark.parametrize("mode", MODES)
def test_cases_e_f_deduplicate_and_block_conflicts(tmp_path: Path, mode: VerificationMode) -> None:
    first, second = registration(tmp_path, name="first"), registration(tmp_path, name="second")
    path, one = registered(tmp_path, first)
    second_id = HistoricalDatasetRegistry(path).register(second).dataset_id
    ids = one + (second_id,)
    with prepared(path, ids, LAUNCH, first.declaration.period_end, mode) as history:
        events = tuple(HistoricalMarkPriceSource(history, publication()).read(0, history.end))
        assert len(events) == len({e.payload.event_id for e in events}) == 3
        assert history.evidence.reconciliation.duplicates == 3
        assert all(e.historical_event.dataset_ids == tuple(sorted(ids)) for e in events)
    third = registration(tmp_path, name="conflict", price="94")
    third_id = HistoricalDatasetRegistry(path).register(third).dataset_id
    with pytest.raises(ValueError, match="CONFLICTED"):
        with prepared(path, ids + (third_id,), LAUNCH, first.declaration.period_end, mode):
            pass


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("provenance", [RegistryProvenance.DECLARED, RegistryProvenance.UNKNOWN])
def test_unauthorized_source_never_publishes(
    tmp_path: Path, mode: VerificationMode, provenance: RegistryProvenance
) -> None:
    reg = registration(tmp_path, provenance=provenance)
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode) as history:
        assert len(tuple(history.read(0, history.end))) == 3
        with pytest.raises(ValueError, match="NOT_AUTHORIZED"):
            HistoricalMarkPriceSource(history, publication())
    with pytest.raises(ValueError, match="OBSERVED"):
        Reference("source", "data", Origin.OBSERVED, "No authentication")
    with pytest.raises(ValueError, match="NOT_AUTHORIZED"):
        publication(publication="MODELED")


@pytest.mark.parametrize(
    "field,value",
    [
        ("latency_ms", -1),
        ("latency_ms", True),
        ("latency_ms", 0.5),
        ("freshness_limit_ms", -1),
        ("freshness_limit_ms", 60000),
        ("freshness_limit_ms", 1.0),
        ("max_events_per_read", 0),
        ("max_events_per_read", 10081),
        ("publication", "OBSERVED"),
        ("publication", "MODELED"),
        ("assumptions", ""),
    ],
)
def test_explicit_policy_validation(field: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        publication(**{field: value})


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("partial", [False, True])
def test_cases_a_b_g_exact_pnl_without_financial_dependencies(
    tmp_path: Path, mode: VerificationMode, partial: bool
) -> None:
    start = AT.replace(second=0, microsecond=0)
    reg = registration(tmp_path, start=start, count=1)
    path, ids = registered(tmp_path, reg)
    ledger = FinancialLedger()
    facts: tuple[ReplayFact, ...] = (execution(1, Side.BUY, "10", "100"),)
    if partial:
        facts += (execution(2, Side.SELL, "5", "110"),)
    position = apply(ledger, *facts)
    with prepared(path, ids, start, start + MINUTE, mode) as history:
        source = HistoricalMarkPriceSource(history, publication())
        performance = FinancialPerformance(policy(capital=None), marks=source)
        at = start + MINUTE
        state = performance.observe(
            performance.initial(START, END), FinancialObservation(at, position)
        )
        for e in source.read(0, at):
            state = performance.observe(state, FinancialObservation(e.received_at, e))
        state = performance.observe(state, FinancialObservation(at, "VALUE"))
        value = performance.report(state, at).current
        assert value.unrealized.value == Decimal("-25" if partial else "-50")
        assert value.gross_realized.value == Decimal("50" if partial else "0")
        assert value.equity.value is None and value.wallet.value is None
        assert value.fees.value is None and value.funding.value is None
        assert value.net_cash.value is None
        stale = at + timedelta(minutes=1)
        state = performance.observe(state, FinancialObservation(stale, "VALUE"))
        assert performance.report(state, stale).current.unrealized.value is None
        performance.validate(state, stale, position)


@pytest.mark.parametrize("mode", MODES)
def test_case_h_risk_with_independent_fixture_evidence(
    tmp_path: Path, mode: VerificationMode
) -> None:
    start = AT.replace(second=0, microsecond=0)
    at = start + MINUTE
    reg = registration(tmp_path, start=start, count=1, price="75")
    path, ids = registered(tmp_path, reg)
    ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    with prepared(path, ids, start, at, mode) as history:
        source = HistoricalMarkPriceSource(history, publication())
        performance = FinancialPerformance(policy(), marks=source)
        template = complete(performance, ledger, price="75", at=at)
        actual_mark = tuple(source.read(0, at))[0]
        state = performance.initial(START, END)
        for observation in template.journal:
            if (
                isinstance(observation.payload, FinancialEvent)
                and getattr(observation.payload.payload, "price_kind", None) == "MARK"
            ):
                observation = replace(observation, payload=actual_mark)
            state = performance.observe(state, observation)
        risk = IsolatedMarginRisk(margin_policy(), FixtureRiskSource((schedule(at=at),)))
        risk_state = risk.observe(
            risk.initial(START, END), RiskObservation(at, risk.context_from(performance, state, at))
        )
        risk_state = risk.observe(risk_state, RiskObservation(at, schedule(at=at)))
        risk_state = risk.observe(risk_state, RiskObservation(at, "VALUE"))
        risk_report = risk.report(risk_state, at)
        result = risk_report.current.positions[0]
        assert result.unrealized.value == Decimal("-250")
        assert result.maintenance.value == Decimal("7.50")
        assert result.buffer.value == Decimal("-7.50")
        assert risk_report.current.survival == "NOT_DETERMINABLE_FROM_SAMPLES"
        assert performance.report(state, at).current.equity.value == Decimal("750")
        risk.validate(risk_state, at, performance, state)


@pytest.mark.parametrize("mode", MODES)
def test_bounded_publication_and_cursor_pages(tmp_path: Path, mode: VerificationMode) -> None:
    reg = registration(tmp_path, count=5)
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode) as history:
        source = HistoricalMarkPriceSource(history, publication(max_events_per_read=2))
        with pytest.raises(ValueError, match="LIMIT_EXCEEDED"):
            next(source.read(0, history.end))
        first = tuple(source.read(0, LAUNCH + 2 * MINUTE))
        second = tuple(source.read(2, LAUNCH + 4 * MINUTE))
        last = tuple(source.read(4, history.end))
        assert [e.sequence for e in first + second + last] == list(range(5))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("column", ["payload", "sequence", "delete", "root", "malformed_json"])
def test_tampering_blocks_without_missing_data_fallback(
    tmp_path: Path, mode: VerificationMode, column: str
) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode) as history:
        source = HistoricalMarkPriceSource(history, publication())
        if column == "root":
            history.blocks = (replace(history.blocks[0], content_digest="0" * 64),)
        else:
            assert history._db is not None
            history._db.close()
            with sqlite3.connect(history.path) as db:
                if column == "delete":
                    db.execute("DELETE FROM marks WHERE sequence=2")
                elif column == "malformed_json":
                    db.execute(
                        "UPDATE marks SET payload=? WHERE sequence=2",
                        ("[" * 2000 + "0" + "]" * 2000,),
                    )
                elif column == "sequence":
                    db.execute("UPDATE marks SET sequence=9 WHERE sequence=2")
                else:
                    text = db.execute("SELECT payload FROM marks WHERE sequence=2").fetchone()[0]
                    db.execute(
                        "UPDATE marks SET payload=? WHERE sequence=2",
                        (text.replace('"95"', '"94"'),),
                    )
            history._open_snapshot()
        with pytest.raises(ValueError, match="MARK_(BLOCK|CURSOR)_CHANGED"):
            tuple(source.read(0, history.end))
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            source.check()


@pytest.mark.parametrize("fault", ["expired", "closed", "source", "session"])
def test_segment_session_invalidates_source(tmp_path: Path, fault: str) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    with session(path, ids) as active:
        with PreparedMarkHistory(
            path,
            ids,
            LAUNCH,
            reg.declaration.period_end,
            cutoff=reg.declaration.period_end,
            mode=VerificationMode.SEALED_SEGMENT_SESSION,
            session=active,
        ) as history:
            source = HistoricalMarkPriceSource(history, publication())
            if fault == "expired":
                active._started -= active.policy.max_seconds + 1
            elif fault == "closed":
                history.close()
            elif fault == "source":
                Path(reg.original_file).write_text("changed")
            else:
                active.close()
            with pytest.raises(ValueError):
                tuple(source.read(0, history.end))


@pytest.mark.parametrize("mode", MODES)
def test_hostile_decimal_and_network_forbidden(
    tmp_path: Path, mode: VerificationMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("OFFLINE: sockets forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    reg = registration(tmp_path, price="0.00012345678901234567890123456789012345678901234567890")
    path, ids = registered(tmp_path, reg)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_UP
        ctx.traps[Inexact] = True
        with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode) as history:
            events = tuple(HistoricalMarkPriceSource(history, publication()).read(0, history.end))
            assert events[0].payload.price == Decimal(
                "0.00012345678901234567890123456789012345678901234567890"
            )
            assert ctx.prec == 2 and ctx.traps[Inexact]


@pytest.mark.parametrize("mode", MODES)
def test_trade_as_mark_and_naive_time_rejected(tmp_path: Path, mode: VerificationMode) -> None:
    reg = registration(tmp_path)
    trade = replace(
        reg,
        declaration=replace(
            reg.declaration,
            dataset_type=DatasetType.TRADE,
            unit_contract="price=USDC/1000SHIB;volume=1000SHIB;taker_buy=1000SHIB",
        ),
    )
    path, ids = registered(tmp_path, trade)
    with pytest.raises(ValueError, match="MARK_DATASETS_REQUIRED"):
        with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode):
            pass
    with pytest.raises(ValueError):
        PreparedMarkHistory(
            path,
            ids,
            LAUNCH.replace(tzinfo=None),
            reg.declaration.period_end,
            cutoff=reg.declaration.period_end,
        )


@pytest.mark.parametrize(
    "contract", [dict(symbol="SHIBUSDC"), dict(settlement_asset="USDT"), dict(market="SPOT")]
)
def test_instrument_contract_is_not_substituted(contract: dict[str, str]) -> None:
    from trading_engine.models.values import Symbol

    kwargs: dict[str, object] = dict(contract)
    if "symbol" in kwargs:
        kwargs["symbol"] = Symbol(contract["symbol"])
    with pytest.raises((TypeError, ValueError)):
        Contract(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", 95.0])
def test_invalid_mark_price_blocks_registration(tmp_path: Path, bad: object) -> None:
    reg = registration(tmp_path)
    import json

    rows = [json.loads(line) for line in Path(reg.original_file).read_text().splitlines()]
    rows[0][4] = bad
    reg = registration(tmp_path, rows=rows)
    with pytest.raises(ValueError):
        registered(tmp_path, reg)


@pytest.mark.parametrize("mode", MODES)
def test_resource_limits_and_same_economic_modes(tmp_path: Path, mode: VerificationMode) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    with pytest.raises(ValueError, match="ROW_LIMIT"):
        with prepared(
            path, ids, LAUNCH, reg.declaration.period_end, mode, limits=AdapterLimits(max_rows=2)
        ):
            pass
    with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode) as first:
        other_mode = MODES[1] if mode is MODES[0] else MODES[0]
        with prepared(path, ids, LAUNCH, reg.declaration.period_end, other_mode) as other:
            one = tuple(HistoricalMarkPriceSource(first, publication()).read(0, first.end))
            two = tuple(HistoricalMarkPriceSource(other, publication()).read(0, other.end))
            assert first.evidence.identity != other.evidence.identity
            assert first.evidence.economic_identity == other.evidence.economic_identity
            assert [
                (e.payload.price, e.payload.effective_at, e.received_at, e.payload.event_id)
                for e in one
            ] == [
                (e.payload.price, e.payload.effective_at, e.received_at, e.payload.event_id)
                for e in two
            ]
            assert first.session is not other.session


@pytest.mark.parametrize("mode", MODES)
def test_runner_continuous_fragmented_and_session_binding(
    tmp_path: Path,
    database: Path,
    quotes: FixtureQuotes,
    replay_request: ReplayRequest,
    facts: FixtureFacts,
    mode: VerificationMode,
) -> None:
    reg = registration(tmp_path, start=START, count=int((END - START) // MINUTE))
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, START, END, mode) as history:
        source = HistoricalMarkPriceSource(history, publication())
        performance = FinancialPerformance(policy(capital=None), marks=source)
        with SQLiteHistory(database) as trade:

            def runner(snapshot: Any = None) -> ReplayRunner:
                return ReplayRunner(
                    trade,
                    replay_request,
                    quotes=quotes,
                    facts=facts,
                    ledger=FinancialLedger(),
                    performance=performance,
                    snapshot=snapshot,
                )

            expected = runner()
            report = expected.run()
            assert report.interruption is None
            for cut in (
                START,
                START + timedelta(seconds=30),
                START + MINUTE,
                T,
                DUE,
                DUE + timedelta(seconds=1),
                END,
            ):
                part = runner()
                part.run(cut)
                resumed = runner(part.snapshot)
                assert resumed.run() == report
                assert resumed.snapshot == expected.snapshot
                assert resumed.run() == report
            with prepared(path, ids, START, END, mode) as other:
                twin = FinancialPerformance(
                    policy(capital=None), marks=HistoricalMarkPriceSource(other, publication())
                )
                with pytest.raises(ValueError, match="misma sesión Mark"):
                    ReplayRunner(
                        trade,
                        replay_request,
                        quotes=quotes,
                        facts=facts,
                        ledger=FinancialLedger(),
                        performance=twin,
                        snapshot=expected.snapshot,
                    )
            pending = runner()
            pending.run(START + MINUTE)
            history.close()
            with pytest.raises(ValueError, match="NOT_AVAILABLE"):
                pending.run()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("fault", ["original", "registry", "segment", "checksum"])
def test_original_registry_segment_and_checksum_tampering(
    tmp_path: Path, mode: VerificationMode, fault: str
) -> None:
    reg = registration(tmp_path)
    if fault == "checksum":
        checksum = tmp_path / "mark.sha256"
        checksum.write_text("0" * 64 + "  mark.jsonl\n")
        reg = replace(reg, checksum_file=str(checksum))
        with pytest.raises(ValueError):
            registered(tmp_path, reg)
        return
    path, ids = registered(tmp_path, reg)
    if fault == "original":
        p = Path(reg.original_file)
        p.write_text(p.read_text().replace('"95"', '"94"'))
    else:
        with sqlite3.connect(path) as db:
            if fault == "registry":
                db.execute("UPDATE datasets SET source_hash=?", ("0" * 64,))
            else:
                db.execute("UPDATE segments SET digest=?", ("0" * 64,))
    with pytest.raises(ValueError):
        with prepared(path, ids, LAUNCH, reg.declaration.period_end, mode):
            pass


@pytest.mark.parametrize("mode", MODES)
def test_mixed_provenance_cannot_be_published(tmp_path: Path, mode: VerificationMode) -> None:
    fixture = registration(tmp_path, name="fixture")
    declared = registration(tmp_path, name="declared", provenance=RegistryProvenance.DECLARED)
    path, first = registered(tmp_path, fixture)
    other = HistoricalDatasetRegistry(path).register(declared).dataset_id
    with prepared(path, first + (other,), LAUNCH, fixture.declaration.period_end, mode) as history:
        with pytest.raises(ValueError, match="PUBLICATION_NOT_AUTHORIZED"):
            FinancialPerformance(policy(), marks=HistoricalMarkPriceSource(history, publication()))


@pytest.mark.parametrize("mode", MODES)
def test_late_mark_is_not_freshened_on_receipt(tmp_path: Path, mode: VerificationMode) -> None:
    start = AT.replace(second=0, microsecond=0)
    reg = registration(tmp_path, start=start)
    path, ids = registered(tmp_path, reg)
    position = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
    with prepared(path, ids, start, reg.declaration.period_end, mode) as history:
        source = HistoricalMarkPriceSource(history, publication(latency_ms=70000))
        event = next(source.read(0, history.end))
        assert event.received_at - event.payload.effective_at == timedelta(milliseconds=70000)
        performance = FinancialPerformance(policy(capital=None), marks=source)
        state = performance.observe(
            performance.initial(START, END), FinancialObservation(event.received_at, position)
        )
        state = performance.observe(state, FinancialObservation(event.received_at, event))
        state = performance.observe(state, FinancialObservation(event.received_at, "VALUE"))
        assert performance.report(state, event.received_at).current.unrealized.value is None


@pytest.mark.parametrize("fault", ["timestamp", "close", "order", "ohlc"])
def test_temporal_and_ohlc_contract_rejected(tmp_path: Path, fault: str) -> None:
    import json

    reg = registration(tmp_path)
    rows = [json.loads(line) for line in Path(reg.original_file).read_text().splitlines()]
    if fault == "timestamp":
        rows[0][0] += 1
    elif fault == "close":
        rows[0][6] += 1
    elif fault == "order":
        rows.reverse()
    else:
        rows[0][2] = "90"
    reg = registration(tmp_path, rows=rows)
    with pytest.raises(ValueError):
        registered(tmp_path, reg)


@pytest.mark.parametrize("cursor", [-1, True, 4, 1.0])
def test_invalid_publication_cursor(tmp_path: Path, cursor: Any) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    with prepared(
        path, ids, LAUNCH, reg.declaration.period_end, VerificationMode.STRICT_FULL
    ) as history:
        source = HistoricalMarkPriceSource(history, publication())
        with pytest.raises(ValueError, match="CURSOR"):
            tuple(source.read(cursor, history.end))


@pytest.mark.parametrize("mode", MODES)
def test_runner_refuses_unbounded_mark_materialization(
    tmp_path: Path, database: Path, replay_request: ReplayRequest, mode: VerificationMode
) -> None:
    reg = registration(tmp_path, start=START, count=int((END - START) // MINUTE))
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, START, END, mode) as history:
        marks = HistoricalMarkPriceSource(history, publication(max_events_per_read=2))
        with SQLiteHistory(database) as trade:
            with pytest.raises(ValueError, match="PUBLICATION_LIMIT_EXCEEDED"):
                ReplayRunner(
                    trade,
                    replay_request,
                    ledger=FinancialLedger(),
                    performance=FinancialPerformance(policy(capital=None), marks=marks),
                )


def test_segment_cache_cannot_publish_provisional_mark(tmp_path: Path) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    with session(path, ids) as active:
        with sqlite3.connect(active.cache_path) as db:
            db.execute("UPDATE locators SET raw_digest=? WHERE sequence=1", ("0" * 64,))
        with pytest.raises(ValueError):
            PreparedMarkHistory(
                path,
                ids,
                LAUNCH,
                reg.declaration.period_end,
                cutoff=reg.declaration.period_end,
                mode=VerificationMode.SEALED_SEGMENT_SESSION,
                session=active,
            )


@pytest.mark.parametrize("error", [TypeError, sqlite3.OperationalError])
def test_prepared_read_error_is_not_missing_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    reg = registration(tmp_path)
    path, ids = registered(tmp_path, reg)
    with prepared(
        path, ids, LAUNCH, reg.declaration.period_end, VerificationMode.STRICT_FULL
    ) as history:
        source = HistoricalMarkPriceSource(history, publication())

        def broken(*args: object) -> Iterator[object]:
            raise error("INDEPENDENT_MARK_READ_FAILURE")
            yield

        monkeypatch.setattr(history, "read", broken)
        with pytest.raises(error, match="INDEPENDENT_MARK_READ_FAILURE"):
            tuple(source.read(0, history.end))


def test_frozen_financial_and_strategy_contracts() -> None:
    import json
    from hashlib import sha256

    data = json.loads(
        (Path(__file__).parent / "fixtures/historical_mark_price_baselines.json").read_text()
    )
    root = Path(__file__).parents[3]
    for file, expected in data["protected_sha256"].items():
        assert sha256((root / file).read_bytes()).hexdigest() == expected, file
    legacy = json.loads(
        (Path(__file__).parent / "fixtures/historical_modeled_baselines.json").read_text()
    )
    assert data["legacy_fingerprints"] == legacy


@pytest.mark.parametrize("mode", MODES)
def test_replay_equity_drawdown_and_complete_financial_snapshot(
    tmp_path: Path,
    database: Path,
    replay_request: ReplayRequest,
    quotes: FixtureQuotes,
    facts: FixtureFacts,
    mode: VerificationMode,
) -> None:
    from trading_engine.replay.financial_performance.evidence import (
        FeeEvidence,
        FixtureFinancialSource,
    )

    from .test_financial_performance import FIXTURE, coverage

    reg = registration(tmp_path, start=START, count=int((END - START) // MINUTE))
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, START, END, mode) as history:
        marks = HistoricalMarkPriceSource(history, publication())
        with SQLiteHistory(database) as trade:
            baseline = ReplayRunner(
                trade, replay_request, quotes=quotes, facts=facts, ledger=FinancialLedger()
            ).run()
            assert baseline.financial_ledger is not None and baseline.interruption is None
            costs: list[FinancialEvent] = []
            for entry in baseline.financial_ledger.entries:
                fee = FeeEvidence(
                    "explicit-zero-fixture-fee:" + str(entry.fill_id),
                    entry.fill_id,
                    entry.order_id,
                    entry.executed_at,
                    Decimal("0"),
                    "USDC",
                    FIXTURE,
                )
                costs.append(FinancialEvent(entry.received_at, len(costs), fee))
            at = START + MINUTE
            while at <= END:
                for kind in ("FUNDING", "TRANSFERS"):
                    costs.append(FinancialEvent(at, len(costs), coverage(kind, at=at)))
                at += MINUTE
            performance = FinancialPerformance(
                policy(), FixtureFinancialSource(tuple(costs)), marks
            )

            def make(snapshot: Any = None) -> ReplayRunner:
                return ReplayRunner(
                    trade,
                    replay_request,
                    quotes=quotes,
                    facts=facts,
                    ledger=FinancialLedger(),
                    performance=performance,
                    snapshot=snapshot,
                )

            continuous = make()
            expected = continuous.run()
            assert expected.interruption is None
            financial = expected.financial_performance
            assert financial is not None
            assert financial.current.equity.value is not None
            assert financial.max_drawdown_usdc.value is not None
            assert financial.max_drawdown_usdc.value > 0
            for cut in (START + MINUTE, DUE, DUE + timedelta(seconds=1), END):
                part = make()
                part.run(cut)
                resumed = make(part.snapshot)
                assert resumed.run() == expected
                assert resumed.snapshot == continuous.snapshot
