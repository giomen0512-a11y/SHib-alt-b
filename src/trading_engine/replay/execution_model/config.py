"""JSON estricto, sin secretos ni valores numéricos implícitos."""

import json
from collections.abc import Mapping
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path

from trading_engine.replay.execution_model.policy import (
    ExecutionPolicy,
    ExecutionProfiles,
    IntraminutePolicy,
    LimitEligibility,
    LiquidityPolicy,
    PartialFillPolicy,
    Scenario,
)


def _object(value: object, keys: set[str]) -> Mapping[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or any(not isinstance(k, str) for k in value)
    ):
        raise ValueError("Campos de configuración ausentes o desconocidos")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("Se requiere texto explícito, sin espacios externos")
    return value


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(_text(value), context=Context())
    except InvalidOperation as exc:
        raise ValueError("Decimal de configuración inválido") from exc


def _milliseconds(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Milisegundos deben ser int, no float/bool")
    return value


def profiles_from_object(value: object) -> ExecutionProfiles:
    root = _object(value, {"schema_version", "profiles"})
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("schema_version no soportado")
    raw = root["profiles"]
    if not isinstance(raw, list):
        raise ValueError("profiles debe ser lista")
    profiles: list[ExecutionPolicy] = []
    keys = {
        "scenario",
        "spread_bps",
        "slippage_bps",
        "quote_latency_ms",
        "freshness_limit_ms",
        "limit_eligibility",
        "liquidity",
        "intraminute",
        "assumptions",
    }
    for item in raw:
        p = _object(item, keys)
        liquidity = _object(p["liquidity"], {"max_volume_participation", "partial_fills"})
        profiles.append(
            ExecutionPolicy(
                Scenario(_text(p["scenario"])),
                _decimal(p["spread_bps"]),
                _decimal(p["slippage_bps"]),
                _milliseconds(p["quote_latency_ms"]),
                _milliseconds(p["freshness_limit_ms"]),
                LimitEligibility(_text(p["limit_eligibility"])),
                LiquidityPolicy(
                    _decimal(liquidity["max_volume_participation"]),
                    PartialFillPolicy(_text(liquidity["partial_fills"])),
                ),
                IntraminutePolicy(_text(p["intraminute"])),
                _text(p["assumptions"]),
            )
        )
    return ExecutionProfiles(tuple(profiles))


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Clave JSON duplicada: " + key)
        result[key] = value
    return result


def load_profiles(path: Path) -> ExecutionProfiles:
    return profiles_from_object(
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    )
