"""Protecciones operativas Linux de un único histórico SQLite."""

import fcntl
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_engine.persistence.futures_sqlite import FuturesSQLite

DEFAULT_RESERVE_BYTES = 512 * 1024 * 1024


class IngestionBusy(RuntimeError):
    """Otro proceso posee el bloqueo; no se ha abierto SQLite para escribir."""


def free_bytes(path: Path) -> int:
    candidate = path.resolve()
    while not candidate.exists():
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free


def require_space(path: Path, reserve: int = DEFAULT_RESERVE_BYTES, *, needed: int = 0) -> int:
    if type(reserve) is not int or reserve < 0 or type(needed) is not int or needed < 0:
        raise ValueError("Reserva y tamaño deben ser enteros no negativos")
    available = free_bytes(path)
    if available < reserve + needed:
        raise OSError(
            f"Espacio insuficiente en {path}: disponible={available}, requerido={reserve + needed}"
        )
    return available


@contextmanager
def exclusive_database(path: Path) -> Iterator[None]:
    """No borrar el lockfile: flock se libera también tras kill/reinicio."""
    lock_path = Path(str(path.resolve()) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IngestionBusy(f"Ingesta ya activa para {path.resolve()}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def consistent_backup(
    store: "FuturesSQLite", target: Path, *, reserve: int = DEFAULT_RESERVE_BYTES
) -> Path:
    """SQLite backup API incluye WAL; publica una copia nueva sin reemplazar datos."""
    target = target.resolve()
    if target == store.path or target.exists():
        raise FileExistsError("El destino debe ser nuevo y distinto de la base de origen")
    pages = store.connection.execute("PRAGMA page_count").fetchone()[0]
    page_size = store.connection.execute("PRAGMA page_size").fetchone()[0]
    require_space(target.parent, reserve, needed=pages * page_size)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=target.parent)
    os.close(descriptor)
    partial = Path(name)
    try:
        with closing(sqlite3.connect(partial)) as destination:
            store.connection.backup(destination, pages=256, sleep=0.1)
            if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Backup SQLite no íntegro")
        with partial.open("rb") as copied:
            os.fsync(copied.fileno())
        os.link(partial, target)  # Atomicidad y rechazo de un destino concurrente existente.
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        partial.unlink(missing_ok=True)
    return target
