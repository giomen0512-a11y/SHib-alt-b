"""Harness fijo offline; subprocess permanece fuera del paquete histórico aprobado."""

import json
import subprocess
import sys
from pathlib import Path

from trading_engine.replay.historical_validation.fast_query_benchmark import QueryMeasurement
from trading_engine.replay.historical_validation.registry import decode_registration
from trading_engine.replay.historical_validation.storage import connection
from trading_engine.replay.historical_validation.verification import checkpoint_from_json


def process_restart_measurement(
    database: Path, checkpoint_file: Path, trusted_root: str
) -> QueryMeasurement:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; "
            "from trading_engine.replay.historical_validation.fast_query_benchmark "
            "import restart_probe; "
            "print(json.dumps(restart_probe(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3])))",
            str(database),
            str(checkpoint_file),
            trusted_root,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    probe = json.loads(child.stdout)
    cp = checkpoint_from_json(checkpoint_file.read_text())
    with connection(database) as db:
        row = db.execute(
            "SELECT spec FROM datasets WHERE id=?", (cp.verification.dataset_ids[0],)
        ).fetchone()
        spec = decode_registration(row[0])
    source_bytes = Path(spec.original_file).stat().st_size
    return QueryMeasurement(
        "J_PROCESS_RESTART",
        "SEALED_SEGMENT_SESSION",
        "COLD_START",
        "FULL_VALIDATION_THEN_SEGMENT_VERIFIED",
        probe["seconds"],
        probe["recognized"],
        probe["causal_digest"],
        probe["peak_rss_kib"],
        probe["python_peak_bytes"],
        source_bytes * 9 + source_bytes // len(spec.segments),
        database.stat().st_size,
        probe["sidecar_bytes"],
    )
