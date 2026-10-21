"""Preparación/publicación de tasas; los motores y la asignación siguen separados."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from trading_engine.binance.klines import LAUNCH, to_ms
from trading_engine.models.enums import Side
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import FinancialEvent, FundingEvidence
from trading_engine.replay.financial_performance.models import FinancialObservation
from trading_engine.replay.historical_validation import (
    funding_history,
    funding_source,
    segment_session,
)
from trading_engine.replay.historical_validation.audit import FundingInventory
from trading_engine.replay.historical_validation.contracts import DatasetType
from trading_engine.replay.historical_validation.funding_history import PreparedFundingHistory
from trading_engine.replay.historical_validation.funding_source import (
    FundingPublicationPolicy,
    HistoricalFundingSource,
)
from trading_engine.replay.historical_validation.query import HistoricalQuery
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.replay_adapter import AdapterLimits
from trading_engine.replay.historical_validation.segments import Registration, RegistryProvenance
from trading_engine.replay.historical_validation.verification import VerificationMode

from .conftest import END, START, SYMBOL
from .test_financial_ledger import apply, execution
from .test_financial_performance import AT, MODELED, RID, complete, coverage, event, mark, policy
from .test_historical_fast_query import session
from .test_historical_registry import spec

T = LAUNCH
STOP = T + timedelta(minutes=3)
MODES = list(VerificationMode)


def row(
    index: int,
    *,
    rate: str = "0.0001",
    available: datetime | None = None,
    price: str | None = "100",
    start: datetime = T,
) -> dict[str, object]:
    at = start + timedelta(minutes=index)
    return {
        "symbol": "1000SHIBUSDC",
        "fundingTime": to_ms(at),
        "fundingRate": rate,
        "markPrice": price,
        "market_available_at": (at if available is None else available).isoformat(),
    }


def inventory(
    *indices: int, start: datetime = T, end: datetime = STOP, available: datetime | None = None
) -> FundingInventory:
    return FundingInventory(
        start,
        end,
        tuple(start + timedelta(minutes=i) for i in indices),
        "Explicit synthetic schedule; not an exchange calendar",
        end if available is None else available,
    )


def registration(
    folder: Path,
    rows: Sequence[object],
    inv: FundingInventory | None,
    *,
    name: str = "funding",
    start: datetime = T,
    end: datetime = STOP,
    provenance: RegistryProvenance = RegistryProvenance.FIXTURE,
) -> Registration:
    result = spec(folder, name, rows, DatasetType.FUNDING, start=start, end=end)
    return replace(
        result,
        funding_inventory=inv,
        provenance=provenance,
        declaration=replace(result.declaration, acquisition_time=end + timedelta(days=1)),
        validation_time=end + timedelta(days=2),
    )


def registered(folder: Path, *registrations: Registration) -> tuple[Path, tuple[str, ...]]:
    registry = HistoricalDatasetRegistry(folder / "registry.sqlite3")
    return registry.path, tuple(registry.register(reg).dataset_id for reg in registrations)


@contextmanager
def prepared(
    path: Path,
    ids: tuple[str, ...],
    inv: FundingInventory | None,
    mode: VerificationMode,
    *,
    start: datetime = T,
    end: datetime = STOP,
    cutoff: datetime | None = None,
    **kwargs: Any,
) -> Iterator[PreparedFundingHistory]:
    if mode is VerificationMode.SEALED_SEGMENT_SESSION:
        with session(path, ids) as active:
            with PreparedFundingHistory(
                path,
                ids,
                start,
                end,
                cutoff=end if cutoff is None else cutoff,
                inventory=inv,
                mode=mode,
                session=active,
                **kwargs,
            ) as history:
                yield history
    else:
        with PreparedFundingHistory(
            path, ids, start, end, cutoff=end if cutoff is None else cutoff, inventory=inv, **kwargs
        ) as history:
            yield history


def publication(**changes: Any) -> FundingPublicationPolicy:
    return replace(
        FundingPublicationPolicy(
            0, 10080, "SYNTHETIC_FIXTURE_ONLY", "Synthetic publication; no Binance latency claim"
        ),
        **changes,
    )


@pytest.mark.parametrize("mode", MODES)
def test_exact_history_inventory_and_no_payment(tmp_path: Path, mode: VerificationMode) -> None:
    inv = inventory(0, 1, 2)
    reg = registration(tmp_path, [row(i) for i in range(3)], inv)
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, inv, mode) as history:
        actual = tuple(history.read(0, STOP))
        expected = tuple(HistoricalQuery(path, ids).read_funding(T, STOP, cutoff=STOP))
        assert tuple(value.event for value in actual) == expected
        assert history.evidence.inventory_status == "COMPLETE"
        assert history.evidence.reference_status == "COMPLETE"
        assert history.evidence.authentication == "NOT_AUTHENTICATED"
        assert history.evidence.economic_authorization == "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
        source = HistoricalFundingSource(history, publication())
        published = tuple(source.read(0, STOP))
        assert [event.sequence for event in published] == [0, 1, 2]
        assert all(not isinstance(event, FinancialEvent) for event in published)
        assert all(event.rate.rate == Decimal("0.0001") for event in published)
        assert all(event.economic_use == "HISTORICAL_RATE_NOT_USER_PAYMENT" for event in published)
        assert source.inventory_at(STOP) is not None
        assert history._db is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            history._db.execute("DELETE FROM funding")
    assert not history.path.exists()
    with pytest.raises(ValueError, match="NOT_AVAILABLE"):
        history.check()


@pytest.mark.parametrize("mode", MODES)
def test_delivery_order_follows_availability_not_effective_time(
    tmp_path: Path, mode: VerificationMode
) -> None:
    inv = inventory(0, 1, 2)
    rows = [row(0, available=T + timedelta(seconds=150)), row(1), row(2)]
    path, ids = registered(tmp_path, registration(tmp_path, rows, inv))
    with prepared(path, ids, inv, mode) as history:
        source = HistoricalFundingSource(history, publication(latency_ms=250))
        assert tuple(source.read(0, T + timedelta(seconds=60))) == ()
        first = tuple(source.read(0, T + timedelta(seconds=60, milliseconds=250)))
        assert len(first) == 1 and first[0].rate.market_effective_at == T + timedelta(minutes=1)
        rest = tuple(source.read(1, STOP))
        assert [event.rate.market_effective_at for event in rest] == [T + timedelta(minutes=2), T]
        assert tuple(source.read(3, STOP)) == ()
        assert source.inventory_at(STOP) is None


@pytest.mark.parametrize("mode", MODES)
def test_late_rate_after_interval_and_inventory_does_not_publish_a_payment(
    tmp_path: Path, mode: VerificationMode
) -> None:
    inv = inventory(0)
    received = STOP + timedelta(hours=1)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0, available=received)], inv))
    with prepared(path, ids, inv, mode, cutoff=received) as history:
        source = HistoricalFundingSource(history, publication())
        assert tuple(source.read(0, STOP)) == ()
        published_inventory = source.inventory_at(STOP)
        assert published_inventory is not None
        assert published_inventory.economic_use == "DECLARED_RATE_INVENTORY_NOT_PAYMENT_COVERAGE"
        event = tuple(source.read(0, received))[0]
        assert event.received_at == received and event.rate.market_effective_at == T
        assert event.historical_event.dataset_ids == ids


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("state", ["missing", "late", "incomplete", "missing_price", "empty"])
def test_inventory_and_reference_sufficiency_are_independent(
    tmp_path: Path, mode: VerificationMode, state: str
) -> None:
    inv = (
        None
        if state == "missing"
        else inventory(
            *(() if state == "empty" else (0, 1)),
            available=STOP + timedelta(seconds=1) if state == "late" else STOP,
        )
    )
    rows = [] if state == "empty" else [row(0)]
    if state in {"missing_price", "late"}:
        rows.append(row(1, price=None if state == "missing_price" else "100"))
    path, ids = registered(tmp_path, registration(tmp_path, rows, inv))
    with prepared(path, ids, inv, mode) as history:
        assert history.evidence.inventory_status == (
            "NOT_AVAILABLE"
            if state in {"missing", "late"}
            else "INCOMPLETE"
            if state == "incomplete"
            else "COMPLETE"
        )
        assert history.evidence.reference_status == (
            "INCOMPLETE" if state == "missing_price" else "COMPLETE"
        )
        source = HistoricalFundingSource(history, publication())
        if state == "empty":
            assert tuple(source.read(0, STOP)) == ()
            assert source.inventory_at(STOP) is not None
        assert all(not isinstance(event, FinancialEvent) for event in source.read(0, STOP))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("contradictory", [False, True])
def test_duplicates_are_combined_and_contradictory_rates_block(
    tmp_path: Path, mode: VerificationMode, contradictory: bool
) -> None:
    inv = inventory(0)
    a = registration(tmp_path, [row(0)], inv, name="a")
    b = registration(
        tmp_path, [row(0, rate="-0.0001" if contradictory else "0.000100")], inv, name="b"
    )
    path, ids = registered(tmp_path, a, b)
    if contradictory:
        with pytest.raises(ValueError, match="CONFLICTED"):
            with prepared(path, ids, inv, mode):
                pass
    else:
        with prepared(path, ids, inv, mode) as history:
            events = tuple(history.read(0, STOP))
            assert len(events) == 1 and events[0].event.duplicates == 1
            assert set(events[0].event.dataset_ids) == set(ids)
            assert history.evidence.inventory_status == "COMPLETE"


@pytest.mark.parametrize("mode", MODES)
def test_incompatible_registered_inventories_block(tmp_path: Path, mode: VerificationMode) -> None:
    a, b = inventory(0), inventory(0, 1)
    path, ids = registered(
        tmp_path,
        registration(tmp_path, [row(0)], a, name="a"),
        registration(tmp_path, [row(0)], b, name="b"),
    )
    with pytest.raises(ValueError, match="INCOMPATIBLE_FUNDING_INVENTORIES"):
        with prepared(path, ids, a, mode):
            pass


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("provenance", [RegistryProvenance.DECLARED, RegistryProvenance.UNKNOWN])
def test_real_declared_history_can_be_prepared_but_not_published(
    tmp_path: Path, mode: VerificationMode, provenance: RegistryProvenance
) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv, provenance=provenance))
    with prepared(path, ids, inv, mode) as history:
        assert len(tuple(history.read(0, STOP))) == 1
        with pytest.raises(ValueError, match="NOT_AUTHORIZED"):
            HistoricalFundingSource(history, publication())


@pytest.mark.parametrize("fault", ["prepared_close", "session_close", "ttl", "source_changed"])
def test_loss_during_one_publication_preserves_returned_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    inv = inventory(0, 1, 2)
    reg = registration(tmp_path, [row(i) for i in range(3)], inv)
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, inv, VerificationMode.SEALED_SEGMENT_SESSION) as history:
        source = HistoricalFundingSource(history, publication())
        stream = source.read(0, STOP)
        prefix = next(stream)
        active = history._source_session
        assert active is not None
        if fault == "prepared_close":
            history.close()
        elif fault == "session_close":
            active.close()
        elif fault == "ttl":
            monkeypatch.setattr(
                segment_session,
                "monotonic",
                lambda: active._started + active.policy.max_seconds + 1,
            )
        else:
            with Path(reg.original_file).open("a") as file:
                file.write("\n")
        with pytest.raises(ValueError, match="NOT_AVAILABLE|SESSION_EXPIRED|SOURCE_CHANGED"):
            next(stream)
        assert prefix.rate.rate == Decimal("0.0001") and prefix.sequence == 0
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            source.inventory_at(STOP)


def test_inventory_checks_trust_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as history:
        source = HistoricalFundingSource(history, publication())
        original = funding_source.FundingInventoryPublication

        def invalidate(*args: Any, **kwargs: Any) -> funding_source.FundingInventoryPublication:
            value = original(*args, **kwargs)
            history.close()
            return value

        monkeypatch.setattr(funding_source, "FundingInventoryPublication", invalidate)
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            source.inventory_at(STOP)


@pytest.mark.parametrize("mode", MODES)
def test_cursor_pages_are_exact_and_economic_ids_match_modes(
    tmp_path: Path, mode: VerificationMode
) -> None:
    inv = inventory(0, 1, 2)
    path, ids = registered(tmp_path, registration(tmp_path, [row(i) for i in range(3)], inv))
    with prepared(path, ids, inv, mode) as history:
        source = HistoricalFundingSource(history, publication())
        continuous = tuple(source.read(0, STOP))
        for cut in [T, T + timedelta(seconds=30), T + timedelta(minutes=1), STOP]:
            first = tuple(source.read(0, cut))
            second = tuple(source.read(len(first), STOP))
            assert first + second == continuous
        with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as other:
            other_events = tuple(HistoricalFundingSource(other, publication()).read(0, STOP))
            assert [v.event_id for v in continuous] == [v.event_id for v in other_events]
            assert history.session is not other.session


@pytest.mark.parametrize(
    "field,value",
    [
        ("latency_ms", -1),
        ("latency_ms", True),
        ("latency_ms", 0.1),
        ("max_events_per_read", 0),
        ("max_events_per_read", 10081),
        ("max_events_per_read", True),
        ("publication", "MODELED"),
        ("publication", "OBSERVED"),
        ("assumptions", ""),
    ],
)
def test_explicit_policy_required(field: str, value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        publication(**{field: value})


@pytest.mark.parametrize("cursor", [-1, True, 4, 1.5])
def test_invalid_cursor_fails(tmp_path: Path, cursor: Any) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as history:
        with pytest.raises(ValueError, match="CURSOR_INVALID"):
            tuple(history.read(cursor, STOP))


def test_limits_fail_before_publishing(tmp_path: Path) -> None:
    inv = inventory(0, 1, 2)
    path, ids = registered(tmp_path, registration(tmp_path, [row(i) for i in range(3)], inv))
    with pytest.raises(ValueError, match="INVENTORY_LIMIT"):
        with prepared(
            path, ids, inv, VerificationMode.STRICT_FULL, limits=AdapterLimits(max_rows=2)
        ):
            pass
    with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as history:
        source = HistoricalFundingSource(history, publication(max_events_per_read=2))
        with pytest.raises(ValueError, match="PUBLICATION_LIMIT_EXCEEDED"):
            next(source.read(0, STOP))


@pytest.mark.parametrize("mode", MODES)
def test_block_root_is_verified_before_decoding(tmp_path: Path, mode: VerificationMode) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with prepared(path, ids, inv, mode) as history:
        assert history._db is not None
        history._db.close()
        history._db = None
        with sqlite3.connect(history.path) as db:
            db.execute("UPDATE funding SET payload='not json'")
        history._open_snapshot()
        with pytest.raises(ValueError, match="FUNDING_BLOCK_CHANGED"):
            tuple(history.read(0, STOP))
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            history.check()


@pytest.mark.parametrize("mode", MODES)
def test_multiple_blocks_and_cache_are_bounded(tmp_path: Path, mode: VerificationMode) -> None:
    end = T + timedelta(minutes=1442)
    inv = inventory(*range(1441), end=end)
    path, ids = registered(
        tmp_path, registration(tmp_path, [row(i) for i in range(1441)], inv, end=end)
    )
    with prepared(path, ids, inv, mode, end=end) as history:
        assert len(history.blocks) == 2
        values = history.read(0, end)
        assert sum(1 for _ in values) == 1441
        assert len(history._rows) == 1
        assert [v.sequence for v in history.read(1439, end)] == [1440, 1441]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("rate,amount", [("0.0001", "-0.10"), ("-0.0001", "0.10")])
def test_manual_financial_contract_example_is_not_dynamic_replay(
    tmp_path: Path, mode: VerificationMode, rate: str, amount: str
) -> None:
    # RoundId, cantidad y coberturas son fixtures manuales independientes.
    # No hay asignador histórico productivo ni conversión automática del inventario.
    effective = AT - timedelta(seconds=1)
    data = {
        "symbol": "1000SHIBUSDC",
        "fundingTime": to_ms(effective),
        "fundingRate": rate,
        "markPrice": "100",
        "market_available_at": effective.isoformat(),
    }
    inv = FundingInventory(START, END, (effective,), "Explicit test calendar", END)
    path, ids = registered(tmp_path, registration(tmp_path, [data], inv, start=START, end=END))
    with prepared(path, ids, inv, mode, start=START, end=END) as history:
        published = tuple(HistoricalFundingSource(history, publication()).read(0, AT))[0]
        reference_price = published.rate.mark_price
        assert reference_price is not None
        payment = FundingEvidence(
            "manual-5d4-proof",
            SYMBOL,
            effective,
            RID,
            Decimal("10"),
            MODELED,
            rate=published.rate.rate,
            reference_price=reference_price,
            reference_price_at=effective,
            positive_rate_pays=True,
        )
        ledger = apply(FinancialLedger(), execution(1, Side.BUY, "10", "100"))
        performance = FinancialPerformance(policy())
        initial = complete(performance, ledger, price="110", funding=payment)
        value = performance.value(initial, AT)
        assert value.funding.value == Decimal(amount)
        assert value.gross_realized.value == 0 and value.fees.value == 0
        assert value.net_cash.value == Decimal(amount)
        assert value.wallet.value == Decimal("1000") + Decimal(amount)
        assert value.equity.value == Decimal("1100") + Decimal(amount)
        later = AT + timedelta(seconds=1)
        updated = initial
        for payload in (
            coverage("FUNDING", at=later, ids=(payment.event_id,)),
            coverage("TRANSFERS", at=later),
            mark("90", at=later),
        ):
            updated = performance.observe(
                updated, FinancialObservation(later, event(payload, at=later))
            )
        updated = performance.observe(updated, FinancialObservation(later, "VALUE"))
        report = performance.report(updated, later)
        assert not updated.conflicts
        assert report.current.equity.value == Decimal("900") + Decimal(amount)
        assert report.max_drawdown_usdc.value == Decimal("200")
        # El historial nunca publicó CoverageEvidence ni FundingEvidence.
        assert not isinstance(published, FinancialEvent)


def test_all_legacy_source_tests_and_fingerprints_remain_frozen() -> None:
    root = Path(__file__).parents[3]
    frozen = json.loads(
        (Path(__file__).parent / "fixtures/historical_funding_baselines.json").read_text()
    )
    fixtures = Path(__file__).parent / "fixtures"
    migration = json.loads((fixtures / "dynamic_funding_source_changes.json").read_text())
    changes = migration["approved_source_changes"]
    assert set(changes) == {
        "src/trading_engine/replay/runner.py",
        "src/trading_engine/replay/models.py",
    }
    for file, expected in frozen["protected_sha256"].items():
        if file in changes:
            change = changes[file]
            assert change["historical_sha256"] == expected
            assert sha256((fixtures / change["archive"]).read_bytes()).hexdigest() == expected
            assert change["current_sha256"] != expected
            assert change["equivalence_tests"]
            expected = change["current_sha256"]
        assert sha256((root / file).read_bytes()).hexdigest() == expected, file


def test_caller_decimal_context_does_not_change_prepared_rate(tmp_path: Path) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0, rate="0.000123456789")], inv))
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as history:
            event = tuple(HistoricalFundingSource(history, publication()).read(0, STOP))[0]
            assert event.rate.rate == Decimal("0.000123456789")


@pytest.mark.parametrize("mode", MODES)
def test_unavailable_future_conflict_does_not_change_valid_rate_prefix(
    tmp_path: Path, mode: VerificationMode
) -> None:
    inv = inventory(0)
    first = registration(tmp_path, [row(0)], inv, name="first")
    second = registration(
        tmp_path, [row(0, rate="-0.0001", available=T + timedelta(minutes=1))], inv, name="later"
    )
    path, ids = registered(tmp_path, first, second)
    cutoff = T + timedelta(seconds=30)
    with prepared(path, ids, inv, mode, cutoff=cutoff) as history:
        values = tuple(HistoricalFundingSource(history, publication()).read(0, cutoff))
        assert len(values) == 1 and values[0].rate.rate == Decimal("0.0001")
        assert history.evidence.inventory_status == "NOT_AVAILABLE"
    with pytest.raises(ValueError, match="CONFLICTED"):
        with prepared(path, ids, inv, mode):
            pass


@pytest.mark.parametrize("mode", MODES)
def test_future_extension_preserves_economic_prefix_and_event_ids(
    tmp_path: Path, mode: VerificationMode
) -> None:
    inv = inventory(0, 1, 2)
    one = registration(tmp_path, [row(0)], inv, name="prefix")
    full = registration(tmp_path, [row(i) for i in range(3)], inv, name="full")
    path, ids = registered(tmp_path, one, full)
    cutoff = T + timedelta(seconds=30)
    values = []
    for dataset_id in ids:
        with prepared(path, (dataset_id,), inv, mode, cutoff=cutoff) as history:
            event = tuple(HistoricalFundingSource(history, publication()).read(0, cutoff))[0]
            values.append((event.event_id, event.sequence, event.received_at, event.rate))
    assert values[0] == values[1]


@pytest.mark.parametrize("mode", MODES)
def test_no_original_or_query_reread_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: VerificationMode
) -> None:
    inv = inventory(0, 1, 2)
    reg = registration(tmp_path, [row(i) for i in range(3)], inv)
    path, ids = registered(tmp_path, reg)
    with prepared(path, ids, inv, mode) as history:
        original_open = Path.open

        def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == Path(reg.original_file):
                raise AssertionError("Original must not be reread per funding event")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_open)
        source = HistoricalFundingSource(history, publication())
        assert len(tuple(source.read(0, STOP))) == 3
        assert source.inventory_at(STOP) is not None


@pytest.mark.parametrize("mode", MODES)
def test_irregular_calendar_has_no_eight_hour_assumption(
    tmp_path: Path, mode: VerificationMode
) -> None:
    stop = T + timedelta(minutes=20)
    inv = inventory(0, 7, 9, 15, end=stop)
    path, ids = registered(
        tmp_path, registration(tmp_path, [row(i) for i in (0, 7, 9, 15)], inv, end=stop)
    )
    with prepared(path, ids, inv, mode, end=stop) as history:
        assert history.evidence.inventory_status == "COMPLETE"
        assert len(tuple(history.read(0, stop))) == 4


@pytest.mark.parametrize("mode", MODES)
def test_close_invalidates_identity_and_inventory_even_for_empty_history(
    tmp_path: Path, mode: VerificationMode
) -> None:
    inv = inventory()
    path, ids = registered(tmp_path, registration(tmp_path, [], inv))
    with prepared(path, ids, inv, mode) as history:
        source = HistoricalFundingSource(history, publication())
        history.close()
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            _ = source.identity
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            _ = source.history_session
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            source.inventory_at(STOP)


@pytest.mark.parametrize("mode", MODES)
def test_until_beyond_cutoff_is_not_a_silent_empty_read(
    tmp_path: Path, mode: VerificationMode
) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with prepared(path, ids, inv, mode) as history:
        source = HistoricalFundingSource(history, publication())
        with pytest.raises(ValueError, match="CUTOFF"):
            tuple(source.read(0, STOP + timedelta(microseconds=1)))
        with pytest.raises(ValueError, match="CUTOFF"):
            source.inventory_at(STOP + timedelta(microseconds=1))


@pytest.mark.parametrize("kind", [DatasetType.MARK, DatasetType.TRADE])
def test_other_dataset_categories_are_rejected(tmp_path: Path, kind: DatasetType) -> None:
    from .test_historical_registry import kline

    reg = spec(tmp_path, "other", [kline(0)], kind)
    path, ids = registered(tmp_path, reg)
    with pytest.raises(ValueError, match="FUNDING_DATASETS_REQUIRED"):
        PreparedFundingHistory(path, ids, T, STOP, cutoff=STOP, inventory=None)


@pytest.mark.parametrize("selection", [(), ("missing",), ("missing", "missing")])
def test_explicit_valid_unique_dataset_ids_are_required(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    inv = inventory(0)
    path, _ = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with pytest.raises(ValueError):
        PreparedFundingHistory(path, selection, T, STOP, cutoff=STOP, inventory=inv)


def test_inventory_must_match_the_explicit_period(tmp_path: Path) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with pytest.raises(ValueError, match="INTERVAL_MISMATCH"):
        PreparedFundingHistory(
            path, ids, T, STOP, cutoff=STOP, inventory=inventory(0, end=STOP + timedelta(minutes=1))
        )


def test_row_and_storage_limits_fail_atomically(tmp_path: Path) -> None:
    path, ids = registered(tmp_path, registration(tmp_path, [row(i) for i in range(3)], None))
    with pytest.raises(ValueError, match="ROW_LIMIT"):
        PreparedFundingHistory(
            path, ids, T, STOP, cutoff=STOP, inventory=None, limits=AdapterLimits(max_rows=2)
        )
    larger_end = T + timedelta(minutes=30)
    path, large_ids = registered(
        tmp_path,
        registration(tmp_path, [row(i) for i in range(30)], None, name="larger", end=larger_end),
    )
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        PreparedFundingHistory(
            path,
            large_ids,
            T,
            larger_end,
            cutoff=larger_end,
            inventory=None,
            limits=AdapterLimits(max_temporary_bytes=16384),
        )


def test_provisional_query_cannot_open_a_prepared_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_engine.replay.historical_validation.fast_query import QueryStream

    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    stage = PreparedFundingHistory._stage

    def provisional(
        self: PreparedFundingHistory,
        stream: Iterator[Any],
        value: FundingInventory | None,
        limits: AdapterLimits,
    ) -> tuple[int, int, str, bool, bool]:
        result = stage(self, stream, value, limits)
        assert isinstance(stream, QueryStream)
        stream.state = "PROVISIONAL"
        return result

    monkeypatch.setattr(PreparedFundingHistory, "_stage", provisional)
    with pytest.raises(ValueError, match="FUNDING_QUERY_NOT_COMPLETE"):
        with prepared(path, ids, inv, VerificationMode.SEALED_SEGMENT_SESSION):
            pass


def test_new_preparation_has_same_deterministic_identity_but_distinct_trust_token(
    tmp_path: Path,
) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as first:
        with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as second:
            assert first.evidence == second.evidence
            assert first.session is not second.session


def test_failed_block_validation_precedes_json_interpretation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inv = inventory(0)
    path, ids = registered(tmp_path, registration(tmp_path, [row(0)], inv))
    with prepared(path, ids, inv, VerificationMode.STRICT_FULL) as history:
        history.blocks = (replace(history.blocks[0], content_digest="0" * 64),)

        def forbidden_decode(value: str) -> Any:
            raise AssertionError("Decoder ran before full block integrity verification")

        monkeypatch.setattr(funding_history, "decode", forbidden_decode)
        with pytest.raises(ValueError, match="FUNDING_BLOCK_CHANGED"):
            tuple(history.read(0, STOP))
