"""Lectura local acotada, sin extracción ZIP ni transporte de red."""

import csv
import io
import json
import re
import stat
import struct
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TextIO

from .contracts import Limits


def file_digest(path: Path, limits: Limits) -> str:
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > limits.max_file_bytes:
        raise ValueError("Archivo no regular o excede max_file_bytes")
    digest = sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            total += len(chunk)
            if total > limits.max_file_bytes:
                raise ValueError("Archivo creció más allá del límite")
            digest.update(chunk)
    return digest.hexdigest()


def expected_checksum(path: Path, archive_name: str, limits: Limits) -> str:
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Checksum debe ser archivo regular")
    if path.stat().st_size > min(limits.max_file_bytes, 4096):
        raise ValueError("Checksum demasiado grande")
    text = path.read_text(encoding="ascii")
    match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^\r\n]+)\n?", text)
    if match is None or match[2] != archive_name:
        raise ValueError("Checksum inválido o de otro archivo")
    return match[1].lower()


def zip_directory_preflight(path: Path, limits: Limits) -> None:
    """Rechaza directorios gigantes antes de que ZipFile los cargue en memoria."""
    with path.open("rb") as raw:
        size = path.stat().st_size
        raw.seek(max(0, size - 65_557))
        tail = raw.read(65_557)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        raise zipfile.BadZipFile("ZIP sin directorio final")
    _, disk, directory_disk, entries_disk, entries, directory_size, offset, comment = (
        struct.unpack_from("<4s4H2LH", tail, at)
    )
    if (
        disk
        or directory_disk
        or entries_disk != 1
        or entries != 1
        or directory_size > limits.max_line_bytes
        or directory_size + offset > size
        or len(tail) - at != 22 + comment
    ):
        raise ValueError("ZIP requiere exactamente un miembro y directorio acotado; sin ZIP64")


@contextmanager
def local_text(path: Path, limits: Limits, *, member: str | None = None) -> Iterator[TextIO]:
    if path.suffix.lower() != ".zip":
        if member is not None:
            raise ValueError("member sólo para ZIP")
        with path.open(encoding="utf-8-sig", newline="") as text:
            yield text
        return
    if member is None:
        raise ValueError("Miembro ZIP explícito requerido")
    name = PurePosixPath(member)
    if name.is_absolute() or ".." in name.parts or "\\" in member or len(name.parts) != 1:
        raise ValueError("Path traversal/miembro ZIP no permitido")
    zip_directory_preflight(path, limits)
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) != 1:
            raise ValueError("ZIP requiere exactamente un miembro declarado")
        info = archive.infolist()[0]
        mode = info.external_attr >> 16
        if (
            info.filename != member
            or info.is_dir()
            or info.flag_bits & 1
            or stat.S_ISLNK(mode)
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or info.file_size > limits.max_uncompressed_bytes
            or info.file_size > max(1, info.compress_size) * limits.max_compression_ratio
        ):
            raise ValueError("ZIP inválido, cifrado, symlink o excede límites")
        with (
            archive.open(info) as raw,
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text,
        ):
            yield text


def bounded_lines(text: TextIO, limits: Limits) -> Iterator[str]:
    total = 0
    while line := text.readline(limits.max_line_bytes + 1):
        size = len(line.encode("utf-8"))
        total += size
        if size > limits.max_line_bytes or total > limits.max_uncompressed_bytes:
            raise ValueError("Límite de línea/datos descomprimidos excedido")
        yield line


def reject_constant(value: str) -> object:
    raise ValueError(f"Constante JSON inválida: {value}")


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Columnas/keys JSON contradictorias")
        result[key] = value
    return result


def json_value(text: str) -> object:
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)


def local_rows(
    path: Path,
    limits: Limits,
    *,
    file_format: str,
    member: str | None = None,
) -> Iterator[object]:
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > limits.max_file_bytes:
        raise ValueError("Archivo no regular o excede max_file_bytes")
    with local_text(path, limits, member=member) as text:
        if file_format == "csv":
            rows: Iterator[object] = iter(csv.reader(bounded_lines(text, limits)))
        elif file_format == "jsonl":
            rows = (json_value(line) for line in bounded_lines(text, limits) if line.strip())
        elif file_format == "json":
            # JSON REST acotado. Para años se exige CSV/JSONL streaming.
            chunks: list[str] = []
            length = 0
            for line in bounded_lines(text, limits):
                length += len(line.encode("utf-8"))
                if length > limits.max_json_bytes:
                    raise ValueError("JSON excede max_json_bytes; usar JSONL/CSV")
                chunks.append(line)
            value = json_value("".join(chunks))
            if not isinstance(value, list):
                raise ValueError("JSON requiere array")
            rows = iter(value)
        else:
            raise ValueError("Formato no implementado")
        for count, row in enumerate(rows, 1):
            if count > limits.max_rows:
                raise ValueError("max_rows excedido")
            yield row
