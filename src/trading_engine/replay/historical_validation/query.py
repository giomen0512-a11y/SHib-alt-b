"""Vistas read-only selladas por conjunto explícito de fuentes y cutoff UTC."""

import heapq
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from trading_engine.binance.klines import LAUNCH, minute_ms
from trading_engine.utils.time import require_utc

from .contracts import DatasetType, utc_text
from .local_files import file_digest, local_rows
from .parsers import HistoricalRecord, parse_record
from .registry import digest, economic_identity, json_text, verify_dataset
from .rules import FeeRules, HistoricalBrackets, RuleQuality, SymbolRules
from .segments import Registration
from .storage import connection


@dataclass(frozen=True)
class HistoricalEvent:
    event_id: str
    market_effective_at: datetime
    market_available_at: datetime
    valid_until: datetime
    record: HistoricalRecord
    dataset_ids: tuple[str, ...]
    sources: tuple[str, ...]
    provenance: tuple[str, ...]
    duplicates: int = 0
    authentication: str = "NOT_AUTHENTICATED"
    economic_authorization: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"

    def economic_key(self) -> tuple[object, ...]:
        return self.market_effective_at, self.market_available_at, self.event_id


@dataclass(frozen=True)
class HistoricalConflict:
    effective_at: datetime
    end: datetime
    dataset_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    reason: str = "CONFLICTED_SAME_TIMESTAMP"


class BlockedHistoricalInterval(ValueError):
    def __init__(self, conflict: HistoricalConflict) -> None:
        self.conflict = conflict
        super().__init__(conflict.reason + ": " + conflict.effective_at.isoformat())


def _dataset_events(
    db: sqlite3.Connection,
    dataset_id: str,
    spec: Registration,
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> Iterator[HistoricalEvent]:
    # SQL temporal por fuente; Trade lee el original mediante locator, sin copiar velas.
    raw_rows = enumerate(
        local_rows(
            Path(spec.original_file), spec.limits, file_format=spec.file_format, member=spec.member
        ),
        1,
    )
    for row in db.execute(
        "SELECT sequence,effective,available,valid_until,event_id,payload,raw_digest FROM records "
        "WHERE dataset_id=? AND effective<? AND valid_until>? AND available<=? "
        "AND effective<=? ORDER BY effective,sequence",
        (dataset_id, end.isoformat(), start.isoformat(), cutoff.isoformat(), cutoff.isoformat()),
    ):
        if (
            spec.declaration.dataset_type
            in {DatasetType.TRADE, DatasetType.MARK, DatasetType.FUNDING}
            and utc_text(row[1]) < start
        ):
            continue
        payload = row[5]
        if payload is None:
            for sequence, original_row in raw_rows:
                if sequence == row[0]:
                    raw = original_row
                    break
            else:
                raise ValueError("TRADE_SOURCE_LOCATOR_MISSING")
        else:
            raw = json.loads(payload)
        record = parse_record(raw, spec.declaration.dataset_type, as_of=cutoff)
        if (
            sha256(json_text(raw).encode()).hexdigest() != row[6]
            or economic_identity(raw, spec.declaration.dataset_type, record) != row[4]
        ):
            raise ValueError("SOURCE_RECORD_CHANGED_DURING_QUERY")
        if isinstance(record, (SymbolRules, HistoricalBrackets, FeeRules)) and (
            record.reference.quality is not RuleQuality.VERIFIED
            or record.reference.market_available_at > record.reference.valid_from
        ):
            raise ValueError("NOT_CAUSALLY_USABLE")
        yield HistoricalEvent(
            row[4],
            utc_text(row[1]),
            utc_text(row[2]),
            utc_text(row[3]),
            record,
            (dataset_id,),
            (spec.declaration.source_url_declared,),
            (spec.provenance.value,),
        )


class HistoricalQuery:
    """Candidato de lectura auditada; no implementa fuentes financieras autorizadas."""

    def __init__(self, path: Path, dataset_ids: tuple[str, ...]) -> None:
        self.path = path.resolve()
        if type(dataset_ids) is not tuple or len(dataset_ids) > 1000:
            raise ValueError("Conjunto de fuentes explícito y acotado requerido")
        if len(set(dataset_ids)) != len(dataset_ids):
            raise ValueError("Dataset repetido en selección")
        self.dataset_ids = tuple(sorted(dataset_ids))
        self.selection_id = digest(self.dataset_ids)

    def groups(
        self,
        kind: DatasetType,
        start: datetime,
        end: datetime,
        *,
        cutoff: datetime,
    ) -> Iterator[HistoricalEvent | HistoricalConflict]:
        for at in (start, end, cutoff):
            require_utc(at)
        if start < LAUNCH or start >= end:
            raise ValueError("Consulta fuera del contrato")
        if kind in {DatasetType.TRADE, DatasetType.MARK}:
            minute_ms(start)
            minute_ms(end)
        with connection(self.path) as db:
            specs = [(identity, verify_dataset(db, identity)) for identity in self.dataset_ids]
            streams = [
                _dataset_events(db, identity, spec, start, end, cutoff)
                for identity, spec in specs
                if spec.declaration.dataset_type is kind
            ]
            merged = heapq.merge(*streams, key=lambda e: (e.market_effective_at, e.event_id))
            pending: HistoricalEvent | None = None
            group: list[HistoricalEvent] = []
            # Grupo acotado por límite de fuentes/filas; no lista de todos los registros.
            for event in merged:
                if group and event.market_effective_at != group[0].market_effective_at:
                    combined = _combine(group)
                    if isinstance(combined, HistoricalConflict):
                        yield combined
                        widest = max(group, key=lambda e: e.valid_until)
                        if pending is None or widest.valid_until > pending.valid_until:
                            pending = widest
                    elif pending is not None and _rules_overlap(pending, combined):
                        yield HistoricalConflict(
                            combined.market_effective_at,
                            min(pending.valid_until, combined.valid_until),
                            tuple(sorted(set(pending.dataset_ids + combined.dataset_ids))),
                            (pending.event_id, combined.event_id),
                            "CONFLICTED_RULE_VALIDITY",
                        )
                        if combined.valid_until > pending.valid_until:
                            pending = combined
                    else:
                        yield combined
                        pending = combined
                    group = []
                group.append(event)
                if len(group) > 10000:
                    raise ValueError("CONFLICT_GROUP_LIMIT_EXCEEDED")
            if group:
                combined = _combine(group)
                if (
                    isinstance(combined, HistoricalEvent)
                    and pending is not None
                    and _rules_overlap(pending, combined)
                ):
                    yield HistoricalConflict(
                        combined.market_effective_at,
                        min(pending.valid_until, combined.valid_until),
                        tuple(sorted(set(pending.dataset_ids + combined.dataset_ids))),
                        (pending.event_id, combined.event_id),
                        "CONFLICTED_RULE_VALIDITY",
                    )
                else:
                    yield combined
            # Fuente alterada durante consumo: el stream no puede sellar un digest exitoso.
            for _, spec in specs:
                if file_digest(Path(spec.original_file), spec.limits) != file_digest_before(
                    spec, db
                ):
                    raise ValueError("ORIGINAL_SOURCE_CHANGED_DURING_QUERY")

    def read(
        self,
        kind: DatasetType,
        start: datetime,
        end: datetime,
        *,
        cutoff: datetime,
    ) -> Iterator[HistoricalEvent]:
        # Preflight completo del intervalo solicitado: ninguna regla solapada se entrega
        # como si toda su vigencia fuera incontrovertida. No materializa el stream.
        for event in self.groups(kind, start, end, cutoff=cutoff):
            if isinstance(event, HistoricalConflict):
                raise BlockedHistoricalInterval(event)
        for event in self.groups(kind, start, end, cutoff=cutoff):
            if isinstance(event, HistoricalConflict):
                raise BlockedHistoricalInterval(event)
            yield event

    def read_trade(
        self, start: datetime, end: datetime, *, cutoff: datetime
    ) -> Iterator[HistoricalEvent]:
        return self.read(DatasetType.TRADE, start, end, cutoff=cutoff)

    def read_mark(
        self, start: datetime, end: datetime, *, cutoff: datetime
    ) -> Iterator[HistoricalEvent]:
        return self.read(DatasetType.MARK, start, end, cutoff=cutoff)

    def read_funding(
        self, start: datetime, end: datetime, *, cutoff: datetime
    ) -> Iterator[HistoricalEvent]:
        return self.read(DatasetType.FUNDING, start, end, cutoff=cutoff)

    def read_rules(
        self,
        kind: DatasetType,
        start: datetime,
        end: datetime,
        *,
        cutoff: datetime,
    ) -> Iterator[HistoricalEvent]:
        if kind not in {DatasetType.SYMBOL_RULES, DatasetType.BRACKETS, DatasetType.FEES}:
            raise ValueError("Categoría de reglas requerida")
        return self.read(kind, start, end, cutoff=cutoff)


def file_digest_before(spec: Registration, db: sqlite3.Connection) -> str:
    # Le SHA attendu reste celui de la source enregistrée, jamais le SHA SQLite courant.
    row = db.execute(
        "SELECT source_hash FROM datasets WHERE spec=?", (encoded_spec(spec),)
    ).fetchone()
    if row is None:
        raise ValueError("SOURCE_IDENTITY_CHANGED")
    return str(row[0])


def encoded_spec(spec: Registration) -> str:
    from trading_engine.replay.encoding import encoded

    return encoded(spec).decode()


def _combine(events: list[HistoricalEvent]) -> HistoricalEvent | HistoricalConflict:
    identities = tuple(sorted({e.event_id for e in events}))
    datasets = tuple(sorted({identity for e in events for identity in e.dataset_ids}))
    first = events[0]
    if len(identities) != 1:
        return HistoricalConflict(
            first.market_effective_at, max(e.valid_until for e in events), datasets, identities
        )
    return HistoricalEvent(
        first.event_id,
        first.market_effective_at,
        first.market_available_at,
        first.valid_until,
        first.record,
        datasets,
        tuple(sorted({s for e in events for s in e.sources})),
        tuple(sorted({p for e in events for p in e.provenance})),
        len(events) - 1,
    )


def _rules_overlap(a: HistoricalEvent, b: HistoricalEvent) -> bool:
    return isinstance(a.record, (SymbolRules, HistoricalBrackets, FeeRules)) and (
        b.market_effective_at < a.valid_until
    )
