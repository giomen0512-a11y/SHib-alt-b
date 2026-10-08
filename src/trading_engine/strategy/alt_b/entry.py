"""Evaluación causal de entrada: no emite señales, acciones ni órdenes."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from trading_engine.models._validation import finite_decimal, nonblank, require_type
from trading_engine.models.market import Candle
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.market import (
    _consecutive_minutes,
    _validate_candles,
    _validate_checkpoint,
    aggregate_15m_block,
    decimal_median,
    rv30m_snapshot,
)
from trading_engine.strategy.alt_b.math import _context

INVALID_REASONS = frozenset(
    {
        "insufficient_history",
        "missing_or_nonconsecutive_minute",
        "checkpoint_not_available",
        "missing_current_taker_buy",
        "zero_current_volume",
        "zero_med_vol_4h",
        "zero_med_vol_24h",
        "rv_snapshot_unavailable",
        "zero_rv_ref_24h",
    }
)


@dataclass(frozen=True)
class EntryEvaluation:
    checkpoint_at: datetime
    data_valid: bool
    entry_allowed: bool
    reason: str
    change_24h: Decimal | None = None
    change_24h_pass: bool | None = None
    range_4h: Decimal | None = None
    range_4h_pass: bool | None = None
    pullback_1h: Decimal | None = None
    pullback_1h_pass: bool | None = None
    v15: Decimal | None = None
    med_vol_4h: Decimal | None = None
    drop_1h: Decimal | None = None
    veto_a: bool | None = None
    buy_ratio_real: Decimal | None = None
    med_vol_24h: Decimal | None = None
    rv30m: Decimal | None = None
    rv_ref_24h: Decimal | None = None
    veto_b: bool | None = None

    def __post_init__(self) -> None:
        _validate_checkpoint(self.checkpoint_at)
        require_type(self.data_valid, bool, "data_valid")
        require_type(self.entry_allowed, bool, "entry_allowed")
        nonblank(self.reason, "reason")
        for name in (
            "change_24h",
            "range_4h",
            "pullback_1h",
            "v15",
            "med_vol_4h",
            "drop_1h",
            "buy_ratio_real",
            "med_vol_24h",
            "rv30m",
            "rv_ref_24h",
        ):
            value = getattr(self, name)
            if self.data_valid or value is not None:
                finite_decimal(value, name)
                if (
                    name in {"range_4h", "v15", "med_vol_4h", "med_vol_24h", "rv30m", "rv_ref_24h"}
                    and value < 0
                ):
                    raise ValueError(f"{name} no puede ser negativo")
                if name == "change_24h" and value <= -1:
                    raise ValueError("change_24h debe ser mayor que -1")
                if name == "drop_1h" and value >= 1:
                    raise ValueError("drop_1h debe ser menor que 1")
                if name == "pullback_1h" and not 0 <= value < 1:
                    raise ValueError("pullback_1h debe estar entre 0 y 1 (exclusivo)")
                if name == "buy_ratio_real" and not 0 <= value <= 1:
                    raise ValueError("buy_ratio_real debe estar entre 0 y 1")
                if (
                    self.data_valid
                    and name in {"v15", "med_vol_4h", "med_vol_24h", "rv_ref_24h"}
                    and value == 0
                ):
                    raise ValueError(f"{name} debe ser positivo para datos válidos")
        for name in ("change_24h_pass", "range_4h_pass", "pullback_1h_pass", "veto_a", "veto_b"):
            flag = getattr(self, name)
            if self.data_valid or flag is not None:
                require_type(flag, bool, name)
        if not self.data_valid:
            if self.entry_allowed or self.reason not in INVALID_REASONS:
                raise ValueError("Datos inválidos requieren entrada bloqueada y razón de datos")
        else:
            allowed = bool(
                self.change_24h_pass
                and self.range_4h_pass
                and self.pullback_1h_pass
                and not self.veto_a
                and not self.veto_b
            )
            reason = (
                "veto_a"
                if self.veto_a
                else "veto_b"
                if self.veto_b
                else "entry_allowed"
                if allowed
                else "filters_failed"
            )
            if self.entry_allowed != allowed or self.reason != reason:
                raise ValueError("Resultado incoherente con filtros y vetos")


def evaluate_entry_checkpoint(
    candles_1m: tuple[Candle, ...], checkpoint_at: datetime, config: AltBConfig
) -> EntryEvaluation:
    _validate_checkpoint(checkpoint_at)
    require_type(config, AltBConfig, "config")
    _validate_candles(candles_1m)
    if any(c.close_time > checkpoint_at for c in candles_1m):
        raise ValueError("No se admiten velas futuras")
    if not candles_1m:
        return EntryEvaluation(checkpoint_at, False, False, "insufficient_history")
    if not any(c.close_time == checkpoint_at for c in candles_1m):
        return EntryEvaluation(checkpoint_at, False, False, "checkpoint_not_available")
    start = checkpoint_at - timedelta(minutes=config.warmup_minutes)
    window = tuple(c for c in candles_1m if c.close_time > start)
    if not any(c.open_time <= start for c in candles_1m):
        return EntryEvaluation(checkpoint_at, False, False, "insufficient_history")
    if len(window) != config.warmup_minutes or not _consecutive_minutes(window, start):
        return EntryEvaluation(checkpoint_at, False, False, "missing_or_nonconsecutive_minute")
    if window[0].symbol != config.symbol:
        raise ValueError("Las velas deben corresponder al símbolo configurado")
    blocks = tuple(
        aggregate_15m_block(window[index : index + 15]) for index in range(0, len(window), 15)
    )
    current = blocks[-1]
    if current.taker_buy_base_volume is None:
        return EntryEvaluation(checkpoint_at, False, False, "missing_current_taker_buy")
    if current.volume == 0:
        return EntryEvaluation(checkpoint_at, False, False, "zero_current_volume")
    med4 = decimal_median(tuple(c.volume for c in blocks[-17:-1]))
    med24 = decimal_median(tuple(c.volume for c in blocks[-97:-1]))
    if med4 == 0:
        return EntryEvaluation(checkpoint_at, False, False, "zero_med_vol_4h")
    if med24 == 0:
        return EntryEvaluation(checkpoint_at, False, False, "zero_med_vol_24h")
    snapshots: list[Decimal] = []
    for block_index in range(len(blocks) - 97, len(blocks)):
        end = (block_index + 1) * 15
        snapshot = rv30m_snapshot(window[end - 31 : end], blocks[block_index].close_time)
        if snapshot is None:
            return EntryEvaluation(checkpoint_at, False, False, "rv_snapshot_unavailable")
        snapshots.append(snapshot)
    rv_ref = decimal_median(tuple(snapshots[:-1]))
    if rv_ref == 0:
        return EntryEvaluation(checkpoint_at, False, False, "zero_rv_ref_24h")
    with localcontext(_context()):
        change = current.close.value / blocks[-97].close.value - Decimal("1")
        low4 = min(c.low.value for c in blocks[-16:])
        range4 = (max(c.high.value for c in blocks[-16:]) - low4) / low4
        pullback = Decimal("1") - current.close.value / max(c.high.value for c in blocks[-4:])
        drop = Decimal("1") - current.close.value / blocks[-5].close.value
        buy_ratio = current.taker_buy_base_volume / current.volume
        change_pass = config.change_24h_min <= change <= config.change_24h_max
        range_pass = range4 >= config.range_4h_min
        pullback_pass = pullback >= config.pullback_1h_min
        veto_a = (
            current.volume >= config.veto_a_volume_multiplier * med4
            and drop >= config.veto_a_drop_1h_min
        )
        veto_b = (
            buy_ratio <= config.veto_b_buy_ratio_max
            and current.volume >= config.veto_b_volume_multiplier * med24
            and snapshots[-1] >= config.veto_b_rv_multiplier * rv_ref
        )
        allowed = change_pass and range_pass and pullback_pass and not veto_a and not veto_b
        reason = (
            "veto_a"
            if veto_a
            else "veto_b"
            if veto_b
            else "entry_allowed"
            if allowed
            else "filters_failed"
        )
        return EntryEvaluation(
            checkpoint_at,
            True,
            allowed,
            reason,
            change,
            change_pass,
            range4,
            range_pass,
            pullback,
            pullback_pass,
            current.volume,
            med4,
            drop,
            veto_a,
            buy_ratio,
            med24,
            snapshots[-1],
            rv_ref,
            veto_b,
        )
