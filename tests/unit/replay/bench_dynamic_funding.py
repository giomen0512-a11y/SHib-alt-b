"""Benchmark del componente, no certificación de ReplayRunner multianual.

Ejecutar offline: PYTHONPATH=src python -m tests.unit.replay.bench_dynamic_funding
Produce JSON con memoria Python profunda (excluye buffers nativos SQLite), pico
tracemalloc del consumidor y tamaño serializado del estado funding del snapshot.
"""

import gc
import json
import resource
import sys
import tempfile
import tracemalloc
from dataclasses import fields, is_dataclass
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from trading_engine.models.enums import Side
from trading_engine.replay.dynamic_funding import (
    FixtureCalendarSource,
    FixtureExposureFinality,
    FixtureFinalitySource,
    FixtureFundingCalendar,
)
from trading_engine.replay.dynamic_funding.core import DynamicFunding, FundingContext
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.historical_validation.funding_source import HistoricalFundingSource
from trading_engine.replay.historical_validation.verification import VerificationMode
from trading_engine.replay.ports import FillEvidence, FixtureFacts

from .conftest import START
from .test_dynamic_funding import buy, dynamic_policy
from .test_historical_funding import inventory, prepared, publication, registered, registration, row


def python_bytes(value: object, seen: set[int] | None = None) -> int:
    """Alcanzabilidad Python, deduplicada; no pretende incluir cachés C de SQLite."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(python_bytes(getattr(value, f.name), seen) for f in fields(value))
    if isinstance(value, dict):
        return size + sum(python_bytes(k, seen) + python_bytes(v, seen) for k, v in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return size + sum(python_bytes(v, seen) for v in value)
    return size


def measure(days: int, incomplete: bool) -> dict[str, Any]:
    gc.collect()
    end = START + timedelta(days=days)
    indices = tuple(day * 1440 + hour * 60 for day in range(days) for hour in (4, 12, 20))
    facts = FixtureFacts(
        tuple(
            buy(
                day * 2 + i + 1, second=day * 86400 + hour * 3600, side=side, round_number=100 + day
            )
            for day in range(days)
            for i, (hour, side) in enumerate(((1, Side.BUY), (23, Side.SELL)))
        )
    )
    finalities = FixtureFinalitySource(
        tuple(
            FixtureExposureFinality(
                START + timedelta(days=day + 1),
                START + timedelta(days=day + 1),
                facts.identity,
                tuple(
                    f.payload.fill.fill_id
                    for f in facts.facts
                    if isinstance(f.payload, FillEvidence)
                    and f.payload.fill.timestamp <= START + timedelta(days=day + 1)
                ),
                "Independent complete daily synthetic executions",
                "SYNTHETIC_FIXTURE_ONLY",
            )
            for day in range(days)
        )
    )
    with tempfile.TemporaryDirectory(prefix="shib-dynamic-bench-") as temp:
        folder = Path(temp)
        inv = inventory(*indices, start=START, end=end)
        selected = indices[:-1] if incomplete else indices
        rows = [row(i, start=START, available=START + timedelta(minutes=i + 120)) for i in selected]
        path, ids = registered(folder, registration(folder, rows, inv, start=START, end=end))
        begun = perf_counter()
        with prepared(
            path, ids, inv, VerificationMode.STRICT_FULL, start=START, end=end
        ) as history:
            source = HistoricalFundingSource(history, publication())
            dynamic = DynamicFunding(
                source,
                dynamic_policy(),
                finalities,
                FixtureCalendarSource(
                    (
                        FixtureFundingCalendar(
                            START,
                            end,
                            inv.expected_timestamps,
                            end,
                            history.evidence.economic_identity,
                            "Closed synthetic endpoint; no event at end",
                            "SYNTHETIC_FIXTURE_ONLY",
                        ),
                    )
                ),
            )
            ledger = FinancialLedger()
            state = dynamic.initial(START, end, facts, None, ledger.identity)
            position = ledger.initial()
            source_bytes = python_bytes(history.__dict__) + python_bytes(source.policy)
            prepare_seconds = perf_counter() - begun
            begun = perf_counter()
            tracemalloc.start()
            index = 0
            now = START
            peak_agenda = 0
            peak_journal = 0
            while True:
                next_funding = dynamic.next_at(state, now, None)
                candidates = ([] if next_funding is None else [next_funding]) + (
                    [] if index == len(facts.facts) else [facts.facts[index].received_at]
                )
                if not candidates or min(candidates) > end:
                    break
                now = min(candidates)
                while index < len(facts.facts) and facts.facts[index].received_at == now:
                    fact = facts.facts[index]
                    dynamic.validate_fact(state, fact, position)
                    position = ledger.receive(position, fact, at=now)
                    assert not position.conflicts
                    if (
                        isinstance(fact.payload, FillEvidence)
                        and fact.payload.order.intent.side is Side.SELL
                    ):
                        position = ledger.reconcile_closed(
                            position, (fact.payload.order.intent.round_id,), at=now
                        )
                    index += 1
                state, _ = dynamic.propose(
                    state,
                    FundingContext(
                        now, START, end, position, facts.identity, now + timedelta(minutes=1)
                    ),
                )
                source_bytes = max(
                    source_bytes, python_bytes(history.__dict__) + python_bytes(source.policy)
                )
                peak_agenda = max(peak_agenda, python_bytes((candidates, state.pending)))
                peak_journal = max(peak_journal, python_bytes(state.journal))
            _, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert len(state.latest) == len(selected)
            assert len(position.round_ledger) == days
            assert all(d.status == "ASSIGNED" and d.quantity == Decimal("10") for d in state.latest)
            assert bool(state.coverage) is not incomplete
            return {
                "scope": "COMPONENT_ONLY_NOT_FULL_RUNNER",
                "days": days,
                "incomplete_inventory": incomplete,
                "rates": len(selected),
                "rounds": days,
                "late_rate_hours": 2,
                "prepare_seconds": round(prepare_seconds, 6),
                "consume_seconds": round(perf_counter() - begun, 6),
                "source_python_bytes": source_bytes,
                "agenda_peak_python_bytes": peak_agenda,
                "journal_peak_python_bytes": peak_journal,
                "funding_snapshot_python_bytes": python_bytes(state),
                "funding_snapshot_json_bytes": len(encoded(state)),
                "consumer_tracemalloc_peak_bytes": traced_peak,
                "process_cumulative_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "coverage": bool(state.coverage),
                "fingerprint": sha256(encoded(state)).hexdigest(),
            }


if __name__ == "__main__":
    for period in (1, 7, 30):
        for missing in (False, True):
            print(json.dumps(measure(period, missing)), flush=True)
