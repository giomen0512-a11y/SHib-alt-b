"""Registro atómico y reproducible de archivos locales validados con 4A-5A."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from trading_engine.binance.klines import CSV_HEADER
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded

from .audit import FundingInventory, _numeric_identity, audit_file
from .contracts import (
    DEFAULT_LIMITS,
    Authentication,
    Contract,
    DatasetDeclaration,
    DatasetType,
    Limits,
    utc_text,
)
from .local_files import file_digest, local_rows
from .parsers import HistoricalRecord, milliseconds, parse_record, record_times
from .rules import FeeRules, HistoricalBrackets, SymbolRules
from .segments import RegisteredDataset, Registration, RegistryProvenance, SegmentBounds
from .sqlite_audit import audit_sqlite
from .storage import connection, initialize


def digest(value: object) -> str:
    return sha256(encoded(value)).hexdigest()


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode_registration(text: str) -> Registration:
    data: dict[str, Any] = json.loads(text)
    declaration = data.pop("declaration")
    contract = declaration.pop("contract")
    contract["symbol"] = Symbol(contract["symbol"]["value"])
    declaration["contract"] = Contract(**contract)
    declaration["dataset_type"] = DatasetType(declaration["dataset_type"])
    for key in ("period_start", "period_end", "acquisition_time"):
        declaration[key] = utc_text(declaration[key])
    data["declaration"] = DatasetDeclaration(**declaration)
    data["validation_time"] = utc_text(data["validation_time"])
    data["provenance"] = RegistryProvenance(data["provenance"])
    data["limits"] = Limits(**data["limits"])
    data["segments"] = tuple(
        SegmentBounds(utc_text(s["start"]), utc_text(s["end"])) for s in data["segments"]
    )
    inventory = data["funding_inventory"]
    if inventory is not None:
        inventory["start"] = utc_text(inventory["start"])
        inventory["end"] = utc_text(inventory["end"])
        inventory["market_available_at"] = utc_text(inventory["market_available_at"])
        inventory["expected_timestamps"] = tuple(
            utc_text(t) for t in inventory["expected_timestamps"]
        )
        data["funding_inventory"] = FundingInventory(**inventory)
    return Registration(**data)


def economic_identity(row: object, kind: DatasetType, record: HistoricalRecord) -> str:
    # La procedencia se conserva por separado, no contamina identidad económica.
    if isinstance(record, (SymbolRules, HistoricalBrackets, FeeRules)):
        reference = replace(
            record.reference,
            source="economic",
            source_version="1",
            checksum=None,
            authentication_status=Authentication.UNKNOWN,
        )
        record = replace(record, reference=reference)
    extra: tuple[object, ...] = ()
    if kind is DatasetType.TRADE:
        from .contracts import exact_decimal

        assert isinstance(row, list)
        extra = (
            exact_decimal(row[7], "quote_volume"),
            milliseconds(row[8]),
            exact_decimal(row[10], "taker_buy_quote_volume"),
            exact_decimal(row[11], "ignore"),
        )
    return digest((kind, _numeric_identity((record, extra))))


@dataclass(frozen=True)
class IndexedRecord:
    sequence: int
    effective: datetime
    available: datetime
    valid_until: datetime
    event_id: str
    raw_digest: str
    payload: str | None

    def values(self, dataset_id: str) -> tuple[object, ...]:
        return (
            dataset_id,
            self.sequence,
            self.effective.isoformat(),
            self.available.isoformat(),
            self.valid_until.isoformat(),
            self.event_id,
            self.raw_digest,
            self.payload,
        )


def indexed_rows(spec: Registration) -> Iterator[IndexedRecord]:
    declaration, previous = spec.declaration, None
    kind = declaration.dataset_type
    for sequence, row in enumerate(
        local_rows(
            Path(spec.original_file), spec.limits, file_format=spec.file_format, member=spec.member
        ),
        1,
    ):
        if sequence == 1 and isinstance(row, list) and tuple(row) == CSV_HEADER:
            continue
        effective, available = record_times(row, kind)
        record = parse_record(row, kind, as_of=spec.validation_time)
        until = (
            record.reference.valid_until
            if isinstance(record, (SymbolRules, HistoricalBrackets, FeeRules))
            else effective + timedelta(minutes=1)
        )
        if (
            not effective < declaration.period_end
            or (
                effective < declaration.period_start
                and not isinstance(record, (SymbolRules, HistoricalBrackets, FeeRules))
            )
            or until <= declaration.period_start
        ):
            raise ValueError("Registro fuera del período declarado")
        if previous is not None and effective < previous:
            raise ValueError("Fuente desordenada; no se ordena arbitrariamente")
        previous = effective
        raw = json_text(row)
        # Trade: índice/locator solamente. El original sigue siendo la única fuente.
        yield IndexedRecord(
            sequence,
            effective,
            available,
            until,
            economic_identity(row, kind, record),
            sha256(raw.encode()).hexdigest(),
            None if kind is DatasetType.TRADE else raw,
        )


def segment_values(
    db: sqlite3.Connection, dataset_id: str, bounds: SegmentBounds
) -> tuple[int, str, int]:
    count, conflicts, previous, previous_id = 0, 0, None, None
    fingerprint = sha256()
    for row in db.execute(
        "SELECT sequence,effective,available,valid_until,event_id,raw_digest,payload "
        "FROM records WHERE dataset_id=? AND effective<? AND valid_until>? "
        "ORDER BY sequence",
        (dataset_id, bounds.end.isoformat(), bounds.start.isoformat()),
    ):
        fingerprint.update(encoded(tuple(row)))
        count += 1
        if previous == row[1] and previous_id != row[4]:
            conflicts += 1
        previous, previous_id = row[1], row[4]
    return count, fingerprint.hexdigest(), conflicts


def segment_coverage(
    db: sqlite3.Connection,
    dataset_id: str,
    bounds: SegmentBounds,
    spec: Registration,
    audit: str,
    conflicts: int,
) -> str:
    if conflicts:
        return "CONFLICTED"
    if spec.declaration.interval == "1m":
        from trading_engine.binance.klines import minute_ms

        unique = db.execute(
            "SELECT count(DISTINCT effective) FROM records WHERE dataset_id=? "
            "AND effective>=? AND effective<?",
            (dataset_id, bounds.start.isoformat(), bounds.end.isoformat()),
        ).fetchone()[0]
        expected = (minute_ms(bounds.end) - minute_ms(bounds.start)) // 60000
        return "MISSING" if not unique else "AVAILABLE" if unique == expected else "PARTIAL"
    # No infiere calendarios funding ni vigencias completas desde conteos de extremos.
    semantic = json.loads(audit)["semantic"]
    return (
        "AVAILABLE"
        if semantic["coverage_complete"] and semantic["causally_usable"]
        else "NOT_VERIFIED"
    )


def audit_registration(spec: Registration) -> str:
    return encoded(
        audit_file(
            Path(spec.original_file),
            spec.declaration,
            validation_time=spec.validation_time,
            as_of=spec.validation_time,
            file_format=spec.file_format,
            limits=spec.limits,
            member=spec.member,
            checksum_file=None if spec.checksum_file is None else Path(spec.checksum_file),
            funding_inventory=spec.funding_inventory,
        )
    ).decode()


def note_import(
    db: sqlite3.Connection,
    attempt_id: str,
    state: str,
    error: str | None,
    at: datetime,
) -> None:
    values = (attempt_id, state, error, at.isoformat())
    db.execute("INSERT OR REPLACE INTO imports VALUES (?,?,?,?)", values)
    # Recuperar no borra el error anterior; repeticiones idénticas son idempotentes.
    db.execute("INSERT OR IGNORE INTO import_events VALUES (?,?,?,?,?)", (digest(values), *values))


class HistoricalDatasetRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        initialize(self.path)

    def register(
        self, registration: Registration, *, before_commit: Callable[[], None] | None = None
    ) -> RegisteredDataset:
        spec = replace(
            registration,
            original_file=str(Path(registration.original_file).resolve()),
            checksum_file=None
            if registration.checksum_file is None
            else str(Path(registration.checksum_file).resolve()),
        )
        if Path(spec.original_file) == self.path or (
            spec.checksum_file is not None and Path(spec.checksum_file) == self.path
        ):
            raise ValueError("Registro debe estar separado de sus fuentes")
        attempt_id = digest(spec)
        try:
            source_hash = file_digest(Path(spec.original_file), spec.limits)
            dataset_id = digest((spec, source_hash))
            audit = audit_registration(spec)
            audit_id = sha256(audit.encode()).hexdigest()
            with connection(self.path, read_only=False) as db:
                old = db.execute("SELECT id FROM datasets WHERE id=?", (dataset_id,)).fetchone()
                if old is not None:
                    verify_dataset(db, dataset_id)
                    return registered(db, dataset_id)
                db.execute(
                    "INSERT INTO datasets VALUES (?,?,?,?,?,?)",
                    (
                        dataset_id,
                        encoded(spec).decode(),
                        source_hash,
                        audit,
                        audit_id,
                        "COMPLETE_IMPORT",
                    ),
                )
                for row in indexed_rows(spec):
                    db.execute(
                        "INSERT INTO records VALUES (?,?,?,?,?,?,?,?)", row.values(dataset_id)
                    )
                # Segmentos sellados, versiones nuevas reciben otro ID, jamás UPDATE.
                for bounds in spec.segments:
                    count, fingerprint, conflicts = segment_values(db, dataset_id, bounds)
                    coverage = segment_coverage(db, dataset_id, bounds, spec, audit, conflicts)
                    db.execute(
                        "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?)",
                        (
                            digest((dataset_id, bounds)),
                            dataset_id,
                            bounds.start.isoformat(),
                            bounds.end.isoformat(),
                            count,
                            fingerprint,
                            coverage,
                            conflicts,
                        ),
                    )
                if before_commit is not None:
                    before_commit()
                if file_digest(Path(spec.original_file), spec.limits) != source_hash:
                    raise ValueError("Archivo cambió durante importación")
                if audit_registration(spec) != audit:
                    raise ValueError("Archivo/checksum cambió durante importación")
                note_import(db, attempt_id, "COMPLETE_IMPORT", None, spec.validation_time)
                result = registered(db, dataset_id)
            return result
        except Exception as exc:
            with connection(self.path, read_only=False) as db:
                note_import(
                    db,
                    attempt_id,
                    "FAILED",
                    type(exc).__name__ + ": " + str(exc),
                    spec.validation_time,
                )
            raise

    def verify(self, dataset_id: str) -> RegisteredDataset:
        with connection(self.path) as db:
            verify_dataset(db, dataset_id)
            return registered(db, dataset_id)

    def register_trade_sqlite_inventory(
        self,
        original_file: Path,
        declaration: DatasetDeclaration,
        *,
        validation_time: datetime,
        provenance: RegistryProvenance,
        provenance_evidence: str,
        limits: Limits = DEFAULT_LIMITS,
    ) -> str:
        """Inventario separado y bloqueado, sin transformar manifiestos globales en feed causal."""
        from trading_engine.models._validation import nonblank, require_type
        from trading_engine.utils.time import require_utc

        require_utc(validation_time)
        require_type(provenance, RegistryProvenance, "provenance")
        nonblank(provenance_evidence, "provenance_evidence")
        if validation_time < declaration.acquisition_time or original_file.resolve() == self.path:
            raise ValueError("Fuente SQLite/fecha incompatibles")
        # Una fuente externa en WAL activo no es un artefacto sellado de un solo archivo.
        if any(Path(str(original_file) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise ValueError("SQLITE_SOURCE_NOT_SEALED")
        with original_file.open("rb") as stream:
            header = stream.read(20)
        if header[18:20] != b"\x01\x01":
            raise ValueError("SQLITE_SOURCE_NOT_SEALED")
        source_hash = file_digest(original_file, limits)
        audit = audit_sqlite(original_file, declaration, as_of=validation_time, limits=limits)
        if file_digest(original_file, limits) != source_hash:
            raise ValueError("SQLITE_SOURCE_CHANGED")
        values = (
            str(original_file.resolve()),
            source_hash,
            encoded(declaration).decode(),
            validation_time.isoformat(),
            provenance.value,
            provenance_evidence,
            encoded(audit).decode(),
        )
        identity = digest(values)
        with connection(self.path, read_only=False) as db:
            old = db.execute(
                "SELECT * FROM external_trade_inventories WHERE id=?", (identity,)
            ).fetchone()
            if old is not None and old != (identity, *values):
                raise ValueError("SQLITE_INVENTORY_ALTERED")
            db.execute(
                "INSERT OR IGNORE INTO external_trade_inventories VALUES (?,?,?,?,?,?,?,?)",
                (identity, *values),
            )
        return identity


def registered(db: sqlite3.Connection, dataset_id: str) -> RegisteredDataset:
    row = db.execute(
        "SELECT audit_id,source_hash FROM datasets WHERE id=?", (dataset_id,)
    ).fetchone()
    if row is None:
        raise ValueError("DATASET_NOT_PROVIDED")
    count = db.execute("SELECT count(*) FROM records WHERE dataset_id=?", (dataset_id,)).fetchone()[
        0
    ]
    segments = tuple(
        r[0]
        for r in db.execute(
            "SELECT id FROM segments WHERE dataset_id=? ORDER BY start", (dataset_id,)
        )
    )
    return RegisteredDataset(dataset_id, row[0], row[1], count, segments)


def verify_dataset(db: sqlite3.Connection, dataset_id: str) -> Registration:
    saved = db.execute(
        "SELECT spec,source_hash,audit,audit_id,state FROM datasets WHERE id=?", (dataset_id,)
    ).fetchone()
    if saved is None:
        raise ValueError("DATASET_NOT_PROVIDED")
    spec = decode_registration(saved[0])
    if saved[4] != "COMPLETE_IMPORT" or digest((spec, saved[1])) != dataset_id:
        raise ValueError("DATASET_IDENTITY_CHANGED")
    if file_digest(Path(spec.original_file), spec.limits) != saved[1]:
        raise ValueError("ORIGINAL_SOURCE_CHANGED")
    audit = audit_registration(spec)
    if audit != saved[2] or sha256(audit.encode()).hexdigest() != saved[3]:
        raise ValueError("AUDIT_CHANGED")
    cursor = iter(
        db.execute("SELECT * FROM records WHERE dataset_id=? ORDER BY sequence", (dataset_id,))
    )
    for record in indexed_rows(spec):
        if next(cursor, None) != record.values(dataset_id):
            raise ValueError("SEGMENT_RECORD_ALTERED")
    if next(cursor, None) is not None:
        raise ValueError("SEGMENT_EXTRA_RECORD")
    for bounds in spec.segments:
        count, fingerprint, conflicts = segment_values(db, dataset_id, bounds)
        coverage = segment_coverage(db, dataset_id, bounds, spec, audit, conflicts)
        row = db.execute(
            "SELECT * FROM segments WHERE id=?", (digest((dataset_id, bounds)),)
        ).fetchone()
        if row != (
            digest((dataset_id, bounds)),
            dataset_id,
            bounds.start.isoformat(),
            bounds.end.isoformat(),
            count,
            fingerprint,
            coverage,
            conflicts,
        ):
            raise ValueError("SEALED_SEGMENT_ALTERED")
    if db.execute("SELECT count(*) FROM segments WHERE dataset_id=?", (dataset_id,)).fetchone()[
        0
    ] != len(spec.segments):
        raise ValueError("SEALED_SEGMENTS_CHANGED")
    if file_digest(Path(spec.original_file), spec.limits) != saved[1]:
        raise ValueError("ORIGINAL_SOURCE_CHANGED")
    return spec
