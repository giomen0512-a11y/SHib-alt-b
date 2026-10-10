"""Conciliación auditable; no elige mensual/diario ni una procedencia preferida."""

import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from hashlib import sha256

from trading_engine.binance.klines import LAUNCH, from_ms, minute_ms, to_ms
from trading_engine.market_data.audit import Gap
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.utils.time import require_utc

from .audit import FundingInventory, _months
from .contracts import DEFAULT_LIMITS, Authentication, DatasetType
from .query import HistoricalConflict, HistoricalQuery
from .registry import decode_registration, digest
from .report import MonthCoverage
from .storage import connection


@dataclass(frozen=True)
class ReconciliationReport:
    selection_id: str
    kind: DatasetType
    start: datetime
    end: datetime
    cutoff: datetime
    requested_end: datetime
    recognized: int
    duplicates: int
    conflicts: tuple[HistoricalConflict, ...]
    gaps: tuple[Gap, ...]
    coverage_status: str
    causal_digest: str
    funding_status: str
    funding_inventory: FundingInventory | None
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"

    def prefix_key(self) -> tuple[object, ...]:
        return (
            self.kind,
            self.start,
            self.end,
            self.cutoff,
            self.recognized,
            self.conflicts,
            self.gaps,
            self.coverage_status,
            self.causal_digest,
        )


@dataclass(frozen=True)
class RegistryCoverageRow:
    coverage: MonthCoverage
    dataset_ids: tuple[str, ...]
    sources: tuple[str, ...]
    checksum_states: tuple[str, ...]
    causal_use: str
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"


class HistoricalReconciliation:
    def __init__(self, query: HistoricalQuery) -> None:
        self.query = query

    def evaluate(
        self,
        kind: DatasetType,
        start: datetime,
        end: datetime,
        *,
        cutoff: datetime,
        funding_inventory: FundingInventory | None = None,
    ) -> ReconciliationReport:
        require_utc(cutoff)
        effective_end = min(end, cutoff)
        klines = kind in {DatasetType.TRADE, DatasetType.MARK}
        if klines:
            effective_end = from_ms(
                minute_ms(end) if cutoff >= end else to_ms(cutoff) // 60000 * 60000
            )
        if effective_end <= start:
            raise ValueError("No hay prefijo disponible")
        cursor, count, duplicates = start, 0, 0
        fingerprint = sha256()
        conflicts: list[HistoricalConflict] = []
        gaps: list[Gap] = []
        inventory = funding_inventory
        inventory_ok = inventory is not None and (
            inventory.start == start
            and inventory.end == effective_end
            and inventory.market_available_at <= cutoff
        )
        funding_matched = 0
        query_end = effective_end if klines else end
        for event in self.query.groups(kind, start, query_end, cutoff=cutoff):
            if isinstance(event, HistoricalConflict):
                conflicts.append(event)
                if len(conflicts) > DEFAULT_LIMITS.max_findings:
                    raise ValueError("max_findings excedido")
                continue
            fingerprint.update(encoded(event.economic_key()))
            count += 1
            duplicates += event.duplicates
            if kind is DatasetType.FUNDING:
                if inventory is not None and inventory_ok:
                    if (
                        funding_matched >= len(inventory.expected_timestamps)
                        or inventory.expected_timestamps[funding_matched]
                        != event.market_effective_at
                    ):
                        inventory_ok = False
                funding_matched += 1
                from .parsers import FundingRate

                assert isinstance(event.record, FundingRate)
                if event.record.mark_price is None:
                    inventory_ok = False
                continue
            begins = max(start, event.market_effective_at)
            if begins > cursor:
                gaps.append(Gap(to_ms(cursor), to_ms(begins)))
            cursor = max(cursor, min(effective_end, event.valid_until))
            if len(gaps) > DEFAULT_LIMITS.max_findings:
                raise ValueError("max_findings excedido")
        if kind is not DatasetType.FUNDING and cursor < effective_end:
            gaps.append(Gap(to_ms(cursor), to_ms(effective_end)))
        inventory_ok = bool(
            inventory_ok
            and inventory is not None
            and funding_matched == len(inventory.expected_timestamps)
        )
        funding_status = (
            "NOT_APPLICABLE"
            if kind is not DatasetType.FUNDING
            else (
                "DECLARED_INVENTORY_MATCHED_NOT_AUTHENTICATED"
                if inventory_ok and count
                else "FUNDING_HISTORY_INCOMPLETE"
            )
        )
        status = (
            "CONFLICTED"
            if conflicts
            else "MISSING"
            if not count
            else (
                "PARTIAL"
                if gaps or (kind is DatasetType.FUNDING and not inventory_ok)
                else "AVAILABLE_FOR_REVIEW"
            )
        )
        return ReconciliationReport(
            self.query.selection_id,
            kind,
            start,
            effective_end,
            cutoff,
            end,
            count,
            duplicates,
            tuple(conflicts),
            tuple(gaps),
            status,
            fingerprint.hexdigest(),
            funding_status,
            funding_inventory if kind is DatasetType.FUNDING else None,
        )

    def persist(
        self,
        report: ReconciliationReport,
        *,
        funding_inventory: FundingInventory | None = None,
    ) -> str:
        if report.selection_id != self.query.selection_id:
            raise ValueError("Informe de otra selección")
        if funding_inventory is None:
            funding_inventory = report.funding_inventory
        if (
            self.evaluate(
                report.kind,
                report.start,
                report.requested_end,
                cutoff=report.cutoff,
                funding_inventory=funding_inventory,
            )
            != report
        ):
            raise ValueError("RECONCILIATION_REPORT_NOT_REPRODUCIBLE")
        identity = digest(report)
        with connection(self.query.path, read_only=False) as db:
            db.execute(
                "INSERT OR IGNORE INTO reconciliations VALUES (?,?)",
                (identity, encoded(report).decode()),
            )
            for conflict in report.conflicts:
                db.execute(
                    "INSERT OR IGNORE INTO conflicts VALUES (?,?,?,?)",
                    (
                        identity,
                        conflict.effective_at.isoformat(),
                        conflict.reason,
                        encoded(conflict.dataset_ids).decode(),
                    ),
                )
        return identity

    def coverage_matrix(
        self,
        start: datetime,
        end: datetime,
        *,
        cutoff: datetime,
        funding_inventories: tuple[FundingInventory, ...] = (),
    ) -> tuple[RegistryCoverageRow, ...]:
        require_utc(start)
        require_utc(end)
        months: list[MonthCoverage] = []
        for left, right in _months(start, end, DEFAULT_LIMITS):
            for kind in (
                DatasetType.TRADE,
                DatasetType.MARK,
                DatasetType.FUNDING,
                DatasetType.SYMBOL_RULES,
                DatasetType.BRACKETS,
                DatasetType.FEES,
            ):
                if left < LAUNCH:
                    months.append(
                        MonthCoverage(
                            left.strftime("%Y-%m"),
                            kind,
                            left,
                            min(right, LAUNCH),
                            0,
                            0,
                            (),
                            0,
                            0,
                            None,
                            "NOT_APPLICABLE",
                            Authentication.UNKNOWN,
                            "PRE_CONTRACT",
                        )
                    )
                begins = max(left, LAUNCH)
                if begins >= right:
                    continue
                expected = (
                    (minute_ms(right) - minute_ms(begins)) // 60000
                    if kind in {DatasetType.TRADE, DatasetType.MARK}
                    else None
                )
                present, duplicate, conflict = 0, 0, 0
                gaps: tuple[Gap, ...] = ()
                percent: Decimal | None = None
                state = "NOT_VERIFIED"
                if begins < min(right, cutoff):
                    try:
                        inventories = [
                            inventory
                            for inventory in funding_inventories
                            if inventory.start == begins and inventory.end == right
                        ]
                        if len(inventories) > 1:
                            raise ValueError("CONFLICTED_FUNDING_INVENTORIES")
                        report = self.evaluate(
                            kind,
                            begins,
                            right,
                            cutoff=cutoff,
                            funding_inventory=inventories[0]
                            if inventories and kind is DatasetType.FUNDING
                            else None,
                        )
                        if (
                            kind is DatasetType.FUNDING
                            and inventories
                            and report.funding_status
                            == "DECLARED_INVENTORY_MATCHED_NOT_AUTHENTICATED"
                        ):
                            expected = len(inventories[0].expected_timestamps)
                        present, duplicate, conflict = (
                            report.recognized,
                            report.duplicates,
                            len(report.conflicts),
                        )
                        gaps = report.gaps
                        state = {"AVAILABLE_FOR_REVIEW": "AVAILABLE"}.get(
                            report.coverage_status, report.coverage_status
                        )
                        if report.end < right and state == "AVAILABLE":
                            state = "PARTIAL"
                        if expected:
                            with localcontext(ledger_context()):
                                percent = Decimal(present) * 100 / Decimal(expected)
                    except ValueError as exc:
                        if str(exc) != "NOT_CAUSALLY_USABLE":
                            raise
                months.append(
                    MonthCoverage(
                        left.strftime("%Y-%m"),
                        kind,
                        begins,
                        right,
                        expected,
                        present,
                        gaps,
                        duplicate,
                        conflict,
                        percent,
                        "IDENTITY_CALCULATED_ONLY",
                        Authentication.UNKNOWN,
                        state,
                    )
                )
        # Metadatos de inventario se mantienen aparte del digest económico causal.
        result: list[RegistryCoverageRow] = []
        with connection(self.query.path) as db:
            metadata = []
            for identity in self.query.dataset_ids:
                saved = db.execute(
                    "SELECT spec,audit FROM datasets WHERE id=?", (identity,)
                ).fetchone()
                if saved is None:
                    raise ValueError("DATASET_NOT_PROVIDED")
                metadata.append((identity, decode_registration(saved[0]), json.loads(saved[1])))
            for month in months:
                applicable = [
                    (identity, spec, audit)
                    for identity, spec, audit in metadata
                    if spec.declaration.dataset_type is month.dataset_type
                    and spec.declaration.period_start < month.end
                    and spec.declaration.period_end > month.start
                ]
                checksums = tuple(
                    sorted(
                        {audit["manifest"]["checksum_verification"] for _, _, audit in applicable}
                    )
                )
                integrity = "INTEGRITY_VALIDATED" if checksums == ("MATCH",) else month.integrity
                result.append(
                    RegistryCoverageRow(
                        replace(month, integrity=integrity),
                        tuple(identity for identity, _, _ in applicable),
                        tuple(
                            sorted(
                                {spec.declaration.source_url_declared for _, spec, _ in applicable}
                            )
                        ),
                        checksums,
                        "CAUSALLY_USABLE_FOR_REVIEW"
                        if month.utilization == "AVAILABLE"
                        else "NOT_CAUSALLY_USABLE",
                    )
                )
        return tuple(result)
