"""SQLite del registro, jamás crea ni modifica tablas del colector."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .segments import SCHEMA_VERSION

SCHEMA = """
CREATE TABLE registry_meta (version INTEGER PRIMARY KEY) STRICT;
CREATE TABLE datasets (
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, source_hash TEXT NOT NULL,
 audit TEXT NOT NULL, audit_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state='COMPLETE_IMPORT')
) STRICT;
CREATE TABLE records (
 dataset_id TEXT NOT NULL REFERENCES datasets(id), sequence INTEGER NOT NULL,
 effective TEXT NOT NULL, available TEXT NOT NULL, valid_until TEXT NOT NULL,
 event_id TEXT NOT NULL, raw_digest TEXT NOT NULL, payload TEXT,
 PRIMARY KEY(dataset_id, sequence)
) STRICT;
CREATE INDEX temporal_records ON records(dataset_id, effective, available);
CREATE TABLE segments (
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
 start TEXT NOT NULL, end TEXT NOT NULL, row_count INTEGER NOT NULL,
 digest TEXT NOT NULL, coverage TEXT NOT NULL, conflicts INTEGER NOT NULL
) STRICT;
CREATE TABLE imports (
 attempt_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('COMPLETE_IMPORT','FAILED')),
 error TEXT, validation_time TEXT NOT NULL
) STRICT;
CREATE TABLE import_events (
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, state TEXT NOT NULL,
 error TEXT, validation_time TEXT NOT NULL
) STRICT;
CREATE TABLE reconciliations (
 id TEXT PRIMARY KEY, report TEXT NOT NULL
) STRICT;
CREATE TABLE external_trade_inventories (
 id TEXT PRIMARY KEY, original_file TEXT NOT NULL, source_hash TEXT NOT NULL,
 declaration TEXT NOT NULL, validation_time TEXT NOT NULL, provenance TEXT NOT NULL,
 provenance_evidence TEXT NOT NULL, audit TEXT NOT NULL
) STRICT;
CREATE TABLE conflicts (
 reconciliation_id TEXT NOT NULL REFERENCES reconciliations(id),
 effective TEXT NOT NULL, reason TEXT NOT NULL, dataset_ids TEXT NOT NULL,
 PRIMARY KEY(reconciliation_id, effective, reason)
) STRICT;
"""


@contextmanager
def connection(path: Path, *, read_only: bool = True) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(
        path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
        uri=True,
        isolation_level=None,
    )
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA cache_size=-1024")
        if read_only:
            db.execute("PRAGMA query_only=ON")
        else:
            db.execute("PRAGMA synchronous=FULL")
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("REGISTRY_SQLITE_CORRUPT")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("REGISTRY_REFERENCE_CORRUPT")
        if db.execute("SELECT version FROM registry_meta").fetchall() != [(SCHEMA_VERSION,)]:
            raise ValueError("REGISTRY_SCHEMA_UNSUPPORTED")
        db.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
        yield db
        if not read_only:
            db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def initialize(path: Path) -> None:
    """Sólo crea una base nueva explícita; rechaza adoptar cualquier esquema ajeno."""
    if path.exists():
        with connection(path):
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Creación exclusiva: no reemplaza fuentes ni bases productivas por accidente.
    with path.open("xb"):
        pass
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.executescript("BEGIN IMMEDIATE;" + SCHEMA)
        db.execute("INSERT INTO registry_meta VALUES (?)", (SCHEMA_VERSION,))
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.close()
        path.unlink()
        raise
    finally:
        db.close()
