"""Configuración explícita sin secretos requeridos en Fase 0."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from trading_engine.models import OperatingMode


@dataclass(frozen=True)
class Settings:
    mode: OperatingMode = OperatingMode.STOPPED

    def __post_init__(self) -> None:
        if self.mode is not OperatingMode.STOPPED:
            raise ValueError("Fase 0 solo admite STOPPED; los demás modos no están implementados")


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if environ is None else environ
    return Settings(mode=OperatingMode(source.get("TRADING_MODE", "STOPPED")))
