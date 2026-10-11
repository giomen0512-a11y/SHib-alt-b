"""Cotizaciones estimadas a partir de minutos ya cerrados; sin red ni fills."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from enum import StrEnum
from functools import cached_property
from hashlib import sha256
from sqlite3 import Error as SQLiteError
from typing import TYPE_CHECKING

from trading_engine.binance.klines import DEFAULT_SYMBOL, minute_ms
from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.market import Candle, Quote
from trading_engine.models.values import Price, Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.replay.execution_model.contracts import price_context
from trading_engine.replay.execution_model.policy import ExecutionPolicy, Scenario
from trading_engine.replay.ports import CandleHistory, QuoteEvidence
from trading_engine.utils.time import require_utc

if TYPE_CHECKING:
    from trading_engine.replay.historical_validation.replay_adapter import PreparedTradeHistory

MINUTE = timedelta(minutes=1)


class PriceOrigin(StrEnum):
    REAL_OBSERVED = "REAL_OBSERVED"
    MODELED = "MODELED_FROM_1M"
    FIXTURE = "SYNTHETIC_FIXTURE"
    UNAVAILABLE = "UNAVAILABLE"


def price_origin(evidence: QuoteEvidence | None) -> PriceOrigin:
    return PriceOrigin.UNAVAILABLE if evidence is None else PriceOrigin(evidence.provenance)


@dataclass(frozen=True)
class ModeledQuoteEvidence(QuoteEvidence):
    reference_candle: Candle
    available_at: datetime
    expires_at: datetime
    scenario: Scenario
    policy_identity: str
    dataset_identity: str

    def __post_init__(self) -> None:
        super().__post_init__()
        require_type(self.reference_candle, Candle, "reference_candle")
        require_type(self.scenario, Scenario, "scenario")
        require_utc(self.available_at)
        require_utc(self.expires_at)
        nonblank(self.policy_identity, "policy_identity")
        nonblank(self.dataset_identity, "dataset_identity")
        if self.provenance != PriceOrigin.MODELED:
            raise ValueError("Quote estimada requiere procedencia MODELED_FROM_1M")
        if (
            self.quote.symbol != self.reference_candle.symbol
            or not self.reference_candle.close_time
            <= self.available_at
            <= self.quote.timestamp
            <= self.expires_at
        ):
            raise ValueError("Quote modelada fuera de sus tiempos/símbolo causales")


@dataclass(frozen=True)
class QuoteEstimate:
    evidence: ModeledQuoteEvidence | None
    reason: str


def model_closed_candle(
    candle: Candle, policy: ExecutionPolicy, *, at: datetime, dataset_identity: str
) -> QuoteEstimate:
    require_type(candle, Candle, "candle")
    require_type(policy, ExecutionPolicy, "policy")
    require_utc(at)
    nonblank(dataset_identity, "dataset_identity")
    if candle.symbol != DEFAULT_SYMBOL:
        return QuoteEstimate(None, "unsupported_symbol")
    minute_ms(candle.open_time)
    minute_ms(candle.close_time)
    if candle.close_time != candle.open_time + MINUTE:
        return QuoteEstimate(None, "invalid_1m_interval")
    if candle.close_time > at:
        return QuoteEstimate(None, "candle_not_closed")
    try:
        available = candle.close_time + timedelta(milliseconds=policy.quote_latency_ms)
        expires = candle.close_time + timedelta(milliseconds=policy.freshness_limit_ms)
    except OverflowError:
        return QuoteEstimate(None, "timestamp_out_of_range")
    if at < available:
        return QuoteEstimate(None, "quote_latency_not_elapsed")
    if at > expires:
        return QuoteEstimate(None, "stale_reference")
    with localcontext(price_context()):
        half_spread = policy.spread_bps / Decimal("20000")
        bid = Price(candle.close.value * (Decimal("1") - half_spread))
        ask = Price(candle.close.value * (Decimal("1") + half_spread))
    quote = Quote(candle.symbol, at, bid, ask)
    evidence = ModeledQuoteEvidence(
        quote,
        PriceOrigin.MODELED.value,
        candle,
        available,
        expires,
        policy.scenario,
        policy.identity,
        dataset_identity,
    )
    return QuoteEstimate(evidence, "modeled_quote_available")


@dataclass(frozen=True)
class ModeledQuotes:
    history: CandleHistory
    policy: ExecutionPolicy
    dataset_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.history, CandleHistory):
            raise TypeError("history requiere el puerto CandleHistory")
        require_type(self.policy, ExecutionPolicy, "policy")
        nonblank(self.dataset_identity, "dataset_identity")
        self._check_prepared()

    @property
    def _prepared_history(self) -> "PreparedTradeHistory | None":
        # El adaptador importa ReplayRequest; evitar un ciclo durante la carga de modelos.
        from trading_engine.replay.historical_validation.replay_adapter import PreparedTradeHistory

        return self.history if isinstance(self.history, PreparedTradeHistory) else None

    def _check_prepared(self, at: datetime | None = None) -> None:
        history = self._prepared_history
        if history is not None:
            # La propiedad verifica cierre, TTL e invalidación de la sesión sellada.
            _ = history.session
            if self.dataset_identity != history.evidence.identity:
                raise ValueError("PREPARED_QUOTE_DATASET_MISMATCH")
            if at is not None:
                if at > history.cutoff:
                    raise ValueError("REPLAY_BEYOND_VERIFIED_CUTOFF")
                if at > history.request.end:
                    raise ValueError("REPLAY_OUTSIDE_PREPARED_REQUEST")

    @cached_property
    def _identity_dataset(self) -> str:
        dataset = self.dataset_identity
        history = self._prepared_history
        if history is not None:
            # Mismos inputs económicos → mismos UUID de ejecución y orden de suma Decimal.
            # El alcance STRICT/SEGMENTED sigue vinculado en dataset_identity de cada Quote;
            # el token de history_session impide heredar confianza entre preparaciones.
            evidence = history.evidence
            dataset = (
                "prepared-trade-inputs-v1:"
                + sha256(
                    encoded(
                        (
                            evidence.request,
                            evidence.query_start,
                            evidence.cutoff,
                            evidence.bindings,
                            evidence.query_digest,
                            evidence.candle_digest,
                        )
                    )
                ).hexdigest()
            )
        return dataset

    @property
    def identity(self) -> str:
        self._check_prepared()
        return (
            "modeled-from-1m-v1:"
            + sha256(
                encoded(
                    (
                        self._identity_dataset,
                        DEFAULT_SYMBOL,
                        self.policy.identity,
                        "last_latency_eligible_close_with_continuity",
                    )
                )
            ).hexdigest()
        )

    @property
    def execution_identity(self) -> str:
        return self.identity

    @property
    def history_session(self) -> object:
        self._check_prepared()
        return self.history.session

    def at(self, symbol: Symbol, at: datetime) -> QuoteEvidence | None:
        return self.estimate(symbol, at).evidence

    def estimate(self, symbol: Symbol, at: datetime) -> QuoteEstimate:
        require_type(symbol, Symbol, "symbol")
        require_utc(at)
        self._check_prepared(at)
        if symbol != DEFAULT_SYMBOL:
            if self._prepared_history is not None:
                raise ValueError("REPLAY_CONTRACT_MISMATCH")
            return QuoteEstimate(None, "unsupported_symbol")
        latest_closed = at.replace(second=0, microsecond=0)
        try:
            eligible = at - timedelta(milliseconds=self.policy.quote_latency_ms)
            reference_close = eligible.replace(second=0, microsecond=0)
            start = reference_close - MINUTE
            expected = start
            selected: Candle | None = None
            # Todo el rango está cerrado <= at; verifica gaps hasta el cierre más reciente.
            # La latencia es del cálculo/entrega de Quote, no un permiso de lectura futura.
            for candle in self.history.read(symbol, start, latest_closed):
                if (
                    candle.open_time != expected
                    or candle.close_time != expected + MINUTE
                    or candle.close_time > at
                    or candle.symbol != symbol
                ):
                    if self._prepared_history is not None and (
                        candle.close_time != candle.open_time + MINUTE
                        or candle.close_time > at
                        or candle.symbol != symbol
                        or candle.open_time < expected
                    ):
                        raise ValueError("PREPARED_QUOTE_INVALID_CANDLE")
                    return QuoteEstimate(None, "gap_or_invalid_data")
                if selected is None:
                    selected = candle
                expected += MINUTE
            if selected is None or expected != latest_closed:
                return QuoteEstimate(None, "gap_or_missing_data")
            return model_closed_candle(
                selected, self.policy, at=at, dataset_identity=self.dataset_identity
            )
        except (ValueError, TypeError, SQLiteError, DecimalException, OverflowError):
            # Una historia verificada no puede perder confianza y aparentar falta de liquidez.
            if self._prepared_history is not None:
                raise
            return QuoteEstimate(None, "invalid_data_or_timestamp_range")
