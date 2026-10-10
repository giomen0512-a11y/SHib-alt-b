"""Contratos opt-in; una raíz de sesión no autentica al proveedor."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trading_engine.models._validation import nonblank
from trading_engine.utils.time import require_utc

from .contracts import DatasetType
from .segments import PARSER_VERSION, SCHEMA_VERSION


class VerificationMode(StrEnum):
    STRICT_FULL = "STRICT_FULL"
    SEALED_SEGMENT_SESSION = "SEALED_SEGMENT_SESSION"


class VerificationState(StrEnum):
    FULL_SOURCE_VERIFIED = "FULL_SOURCE_VERIFIED"
    SESSION_VERIFIED = "SESSION_VERIFIED"
    SEGMENT_VERIFIED = "SEGMENT_VERIFIED"
    SOURCE_NOT_REVALIDATED = "SOURCE_NOT_REVALIDATED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    VERIFICATION_NOT_AVAILABLE = "VERIFICATION_NOT_AVAILABLE"


@dataclass(frozen=True)
class SessionPolicy:
    max_seconds: int
    max_queries: int
    source_stability_assumption: str

    def __post_init__(self) -> None:
        for value in (self.max_seconds, self.max_queries):
            if type(value) is not int or value <= 0:
                raise ValueError("Límites explícitos de sesión requeridos")
        nonblank(self.source_stability_assumption, "source_stability_assumption")


@dataclass(frozen=True)
class VerificationCheckpoint:
    selection_id: str
    dataset_ids: tuple[str, ...]
    audit_ids: tuple[str, ...]
    source_digests: tuple[str, ...]
    segment_ids: tuple[str, ...]
    segment_digests: tuple[str, ...]
    verified_ranges: tuple[tuple[datetime, datetime], ...]
    verification_mode: VerificationMode
    verification_scope: str
    session_identity: str
    root_id: str
    source_state: VerificationState
    original_state: VerificationState
    schema_version: int = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION


@dataclass(frozen=True)
class QueryRecoveryCheckpoint:
    verification: VerificationCheckpoint
    kind: DatasetType
    start: datetime
    end: datetime
    cutoff: datetime
    cursor: int
    source_cursors: tuple[tuple[str, int], ...]
    current_segment: str | None
    last_verified_event: str | None
    causal_digest: str
    preflight_digest: str
    conflicts: tuple[str, ...]
    stream_state: str
    checkpoint_digest: str

    def __post_init__(self) -> None:
        for at in (self.start, self.end, self.cutoff):
            require_utc(at)
        if type(self.cursor) is not int or self.cursor < 0:
            raise ValueError("Cursor inválido")


def checkpoint_from_json(text: str) -> QueryRecoveryCheckpoint:
    """Decodificar no otorga confianza; resume exige raíz independiente o emisión local."""
    import json
    from typing import Any

    from .contracts import utc_text
    from .local_files import json_value

    if type(text) is not str or len(text.encode()) > 1024 * 1024:
        raise ValueError("CHECKPOINT_SIZE_LIMIT")
    value = json_value(text)
    if not isinstance(value, dict):
        raise ValueError("CHECKPOINT_OBJECT_REQUIRED")
    data: dict[str, Any] = value
    verification = data.pop("verification")
    verification["verification_mode"] = VerificationMode(verification["verification_mode"])
    for key in ("source_state", "original_state"):
        verification[key] = VerificationState(verification[key])
    for key in ("dataset_ids", "audit_ids", "source_digests", "segment_ids", "segment_digests"):
        if not isinstance(verification[key], list) or len(verification[key]) > 1000:
            raise ValueError("CHECKPOINT_COLLECTION_LIMIT")
        verification[key] = tuple(verification[key])
    if len(verification["verified_ranges"]) > 1000:
        raise ValueError("CHECKPOINT_COLLECTION_LIMIT")
    verification["verified_ranges"] = tuple(
        (utc_text(start), utc_text(end)) for start, end in verification["verified_ranges"]
    )
    data["verification"] = VerificationCheckpoint(**verification)
    data["kind"] = DatasetType(data["kind"])
    for key in ("start", "end", "cutoff"):
        data[key] = utc_text(data[key])
    if len(data["source_cursors"]) > 1000 or len(data["conflicts"]) > 1000:
        raise ValueError("CHECKPOINT_COLLECTION_LIMIT")
    data["source_cursors"] = tuple(tuple(row) for row in data["source_cursors"])
    data["conflicts"] = tuple(data["conflicts"])
    # JSON ya fue comprobado sin NaN ni keys duplicadas; nunca pickle/eval.
    json.dumps(data["source_cursors"], allow_nan=False)
    return QueryRecoveryCheckpoint(**data)
