"""Comprobación local de configuración; no inicia servicios ni opera."""

import json
import logging
from datetime import UTC, datetime

from trading_engine.config import load_settings


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": "INFO",
                "event": "bootstrap_ready",
                "mode": settings.mode.value,
            }
        )
    )


if __name__ == "__main__":
    main()
