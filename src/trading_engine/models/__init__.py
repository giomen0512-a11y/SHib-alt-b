"""Tipos compartidos, independientes de proveedores externos."""

from enum import StrEnum


class OperatingMode(StrEnum):
    STOPPED = "STOPPED"
    REPLAY = "REPLAY"
    SHADOW = "SHADOW"
    LIVE_ARMED = "LIVE_ARMED"
    LIVE = "LIVE"
    SAFE_MODE = "SAFE_MODE"
    ERROR = "ERROR"
