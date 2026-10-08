"""Ingesta pura de eventos 1m cerrados; el llamador conserva el estado."""

from dataclasses import dataclass
from enum import StrEnum

from trading_engine.models._validation import require_type
from trading_engine.models.market import Candle
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.entry import EntryEvaluation, evaluate_entry_checkpoint
from trading_engine.strategy.alt_b.market import _consecutive_minutes, _validate_candles


@dataclass(frozen=True)
class AltBMarketState:
    candles_1m: tuple[Candle, ...] = ()

    def __post_init__(self) -> None:
        _validate_candles(self.candles_1m)
        # warmup_minutes es canónico y congelado para todas las AltBConfig válidas.
        if len(self.candles_1m) > AltBConfig().warmup_minutes:
            raise ValueError("El historial supera warmup_minutes")
        if self.candles_1m and not _consecutive_minutes(
            self.candles_1m, self.candles_1m[0].open_time
        ):
            raise ValueError("El estado requiere minutos normalizados consecutivos de un símbolo")


class MarketIngestResult(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    GAP_RESET = "gap_reset"


@dataclass(frozen=True)
class AltBMarketStep:
    state: AltBMarketState
    evaluation: EntryEvaluation | None
    result: MarketIngestResult

    def __post_init__(self) -> None:
        require_type(self.state, AltBMarketState, "state")
        require_type(self.result, MarketIngestResult, "result")
        if self.evaluation is not None:
            require_type(self.evaluation, EntryEvaluation, "evaluation")
        if not self.state.candles_1m:
            raise ValueError("Un paso requiere al menos una vela incorporada")
        if self.result is MarketIngestResult.GAP_RESET and len(self.state.candles_1m) != 1:
            raise ValueError("gap_reset debe reiniciar desde una sola vela")
        last = self.state.candles_1m[-1]
        is_checkpoint = last.close_time.minute % 15 == 0
        should_evaluate = is_checkpoint and self.result is not MarketIngestResult.DUPLICATE
        if (self.evaluation is not None) != should_evaluate:
            raise ValueError("Evaluación incompatible con el checkpoint o resultado de ingesta")
        if self.evaluation is not None:
            if self.evaluation.checkpoint_at != last.close_time:
                raise ValueError("La evaluación debe corresponder a la última vela incorporada")


def ingest_closed_1m(state: AltBMarketState, candle: Candle, config: AltBConfig) -> AltBMarketStep:
    """No consulta reloj: cada evento cerrado y su orden son responsabilidad del llamador."""
    require_type(state, AltBMarketState, "state")
    require_type(candle, Candle, "candle")
    require_type(config, AltBConfig, "config")
    if candle.symbol != config.symbol:
        raise ValueError("La vela debe corresponder al símbolo configurado")
    if not _consecutive_minutes((candle,), candle.open_time):
        raise ValueError("La vela debe ser un minuto UTC normalizado")
    if state.candles_1m and state.candles_1m[0].symbol != config.symbol:
        raise ValueError("El estado debe corresponder al símbolo configurado")

    result = MarketIngestResult.ACCEPTED
    candles: tuple[Candle, ...]
    if state.candles_1m:
        last = state.candles_1m[-1]
        if candle.open_time == last.open_time:
            if candle != last:
                raise ValueError("El mismo intervalo no admite contenido diferente")
            return AltBMarketStep(state, None, MarketIngestResult.DUPLICATE)
        if candle.open_time < last.close_time:
            raise ValueError(
                "Vela fuera de orden; no se admiten correcciones históricas implícitas"
            )
        if candle.open_time > last.close_time:
            candles = (candle,)
            result = MarketIngestResult.GAP_RESET
        else:
            candles = (*state.candles_1m, candle)[-config.warmup_minutes :]
    else:
        candles = (candle,)

    new_state = AltBMarketState(candles)
    close = candle.close_time
    evaluation = (
        evaluate_entry_checkpoint(new_state.candles_1m, close, config)
        if close.minute % 15 == 0 and close.second == close.microsecond == 0
        else None
    )
    return AltBMarketStep(new_state, evaluation, result)
