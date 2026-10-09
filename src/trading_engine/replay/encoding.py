"""Encoding canónico para informes/huellas; no es un formato de recovery."""

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID


def canonical(value: object) -> object:
    if isinstance(value, (datetime, Decimal, UUID)):
        return str(value) if not isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [canonical(v) for v in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"Tipo sin encoding canónico: {type(value).__name__}")


def encoded(value: object) -> bytes:
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
