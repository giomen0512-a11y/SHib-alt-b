"""Benchmark local opt-in; una muestra/minuto es estrés sintético, no calendario Binance."""

import argparse
import json
import resource
import tempfile
import tracemalloc
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from time import perf_counter

from trading_engine.binance.klines import LAUNCH, to_ms
from trading_engine.replay.historical_validation.audit import FundingInventory
from trading_engine.replay.historical_validation.contracts import (
    Contract,
    DatasetDeclaration,
    DatasetType,
    units,
)
from trading_engine.replay.historical_validation.funding_history import PreparedFundingHistory
from trading_engine.replay.historical_validation.funding_source import (
    FundingPublicationPolicy,
    HistoricalFundingSource,
)
from trading_engine.replay.historical_validation.registry import HistoricalDatasetRegistry
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.segments import (
    Registration,
    RegistryProvenance,
    SegmentBounds,
)
from trading_engine.replay.historical_validation.verification import SessionPolicy, VerificationMode


def benchmark(days: int, mode: VerificationMode, incomplete: bool) -> dict[str, object]:
    count = days * 1440
    end = LAUNCH + timedelta(days=days)
    cutoff = end + timedelta(minutes=2)
    inventory = FundingInventory(
        LAUNCH,
        end,
        tuple(LAUNCH + timedelta(minutes=i) for i in range(count)),
        "Synthetic stress calendar, NOT Binance cadence",
        end + timedelta(minutes=1),
    )
    with tempfile.TemporaryDirectory(prefix="shib-funding-benchmark-") as temporary:
        folder = Path(temporary)
        original = folder / "rates.jsonl"
        with original.open("w") as stream:
            for i in range(count):
                if incomplete and i == count // 2:
                    continue
                at = LAUNCH + timedelta(minutes=i)
                stream.write(
                    json.dumps(
                        {
                            "symbol": "1000SHIBUSDC",
                            "fundingTime": to_ms(at),
                            "fundingRate": "0.0001",
                            "markPrice": "100",
                            "market_available_at": (
                                at + timedelta(seconds=(i % 5) * 20)
                            ).isoformat(),
                        }
                    )
                    + "\n"
                )
        registry = HistoricalDatasetRegistry(folder / "registry.sqlite3")
        registration = Registration(
            DatasetDeclaration(
                "synthetic-funding-stress",
                DatasetType.FUNDING,
                Contract(),
                "EVENTS",
                LAUNCH,
                end,
                "local:synthetic-funding",
                "Synthetic only, not official",
                end + timedelta(days=1),
                "historical-local-v1",
                units(DatasetType.FUNDING),
            ),
            str(original),
            "jsonl",
            end + timedelta(days=2),
            RegistryProvenance.FIXTURE,
            "Locally generated stress fixture; no Binance observations",
            (SegmentBounds(LAUNCH, end),),
            funding_inventory=inventory,
        )
        started = perf_counter()
        dataset = registry.register(registration)
        registration_seconds = perf_counter() - started
        with ExitStack() as stack:
            session = None
            if mode is VerificationMode.SEALED_SEGMENT_SESSION:
                session = stack.enter_context(
                    SealedSegmentSession(
                        registry.path,
                        (dataset.dataset_id,),
                        session_identity="fixture-funding-benchmark",
                        opened_at=end + timedelta(days=2),
                        policy=SessionPolicy(3600, 1000, "Controlled local synthetic benchmark"),
                    )
                )
            tracemalloc.start()
            begun = perf_counter()
            history = stack.enter_context(
                PreparedFundingHistory(
                    registry.path,
                    (dataset.dataset_id,),
                    LAUNCH,
                    end,
                    cutoff=cutoff,
                    inventory=inventory,
                    mode=mode,
                    session=session,
                )
            )
            preparation_seconds = perf_counter() - begun
            _, peak_preparation = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            source = HistoricalFundingSource(
                history,
                FundingPublicationPolicy(
                    250,
                    1442,
                    "SYNTHETIC_FIXTURE_ONLY",
                    "Local synthetic stress publication",
                ),
            )
            cursor = 0
            begun = perf_counter()
            for day in range(1, days + 1):
                until = min(cutoff, LAUNCH + timedelta(days=day, minutes=2))
                # Una página de estrés admite hasta 1442 muestras, incluida la latencia.
                # El cursor procede de eventos realmente entregados, no de días inferidos.
                for event in source.read(cursor, min(until, cutoff)):
                    cursor = event.sequence + 1
            _, peak_delivery = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert cursor == count - int(incomplete)
            assert history.evidence.inventory_status == ("INCOMPLETE" if incomplete else "COMPLETE")
            return {
                "days": days,
                "mode": mode.value,
                "incomplete": incomplete,
                "expected_rates": count,
                "delivered_rates": cursor,
                "registration_seconds": registration_seconds,
                "preparation_seconds": preparation_seconds,
                "delivery_seconds": perf_counter() - begun,
                "preparation_python_peak_bytes": peak_preparation,
                "delivery_python_peak_bytes": peak_delivery,
                "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "prepared_bytes": history.path.stat().st_size,
                "blocks": len(history.blocks),
                "inventory_status": history.evidence.inventory_status,
                "consumer": "stream/count only; no financial journal or ReplayRunner",
                "authentication": "NOT_AUTHENTICATED",
                "economic_authorization": "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES",
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, choices=(1, 7, 30), required=True)
    parser.add_argument("--mode", choices=[m.value for m in VerificationMode], required=True)
    parser.add_argument("--incomplete", action="store_true")
    args = parser.parse_args()
    print(json.dumps(benchmark(args.days, VerificationMode(args.mode), args.incomplete)))


if __name__ == "__main__":
    main()
