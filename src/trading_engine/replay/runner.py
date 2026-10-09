"""Runner causal offline: un único AltBEngine/StrategyCoordinator, sin ejecución."""

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from sqlite3 import Error as SQLiteError

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, MARKET
from trading_engine.models.market import Candle
from trading_engine.models.orders import Order
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinationResult
from trading_engine.replay.clock import SimulatedClock
from trading_engine.replay.encoding import encoded
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.models import (
    InvalidPeriod,
    ReplayEmission,
    ReplayReport,
    ReplayRequest,
    ReplaySnapshot,
)
from trading_engine.replay.ports import (
    EntryControl,
    FactSource,
    FillEvidence,
    FixtureFacts,
    NoQuotes,
    QuoteEvidence,
    QuoteSource,
    ReplayFact,
)
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.engine import AltBEngine
from trading_engine.strategy.alt_b.events.state import AltBStrategyState
from trading_engine.utils.time import require_utc

MINUTE = timedelta(minutes=1)


class ReplayRunner:
    def __init__(
        self,
        history: SQLiteHistory,
        request: ReplayRequest,
        *,
        config: AltBConfig | None = None,
        quotes: QuoteSource | None = None,
        facts: FactSource | None = None,
        snapshot: ReplaySnapshot | None = None,
    ) -> None:
        config = AltBConfig() if config is None else config
        quotes = NoQuotes() if quotes is None else quotes
        facts = FixtureFacts() if facts is None else facts
        self.history = history
        self.quotes = quotes
        self.facts = facts
        self.coordinator = StrategyCoordinator(AltBEngine(config))
        start = request.warmup_start(config)
        initial = ReplaySnapshot(
            request,
            config,
            history.session,
            quotes.identity,
            facts.identity,
            SimulatedClock(start),
            start,
        )
        self._snapshot = initial if snapshot is None else snapshot
        if snapshot is not None:
            if (
                snapshot.request,
                snapshot.config,
                snapshot.history_session,
                snapshot.quote_identity,
                snapshot.fact_identity,
            ) != (request, config, history.session, quotes.identity, facts.identity):
                raise ValueError(
                    "Reanudación requiere la misma sesión SQLite/configuración/fuentes"
                )
            self._validate_snapshot()
        else:
            self._record("session", (request, config))
            conflicts = history.conflicts(config.symbol)
            if conflicts:
                self._snapshot = replace(
                    self._snapshot, interruption="sqlite_conflicts:" + " | ".join(conflicts)
                )
                self._record("interruption", self._snapshot.interruption)

    @property
    def snapshot(self) -> ReplaySnapshot:
        return replace(self._snapshot, integrity_digest=self._snapshot.calculate_integrity())

    def _validate_snapshot(self) -> None:
        s = self._snapshot
        if s.integrity_digest != s.calculate_integrity():
            raise ValueError("Integridad de snapshot inválida; conservar estado completo")
        if not s.request.warmup_start(s.config) <= s.clock.now <= s.request.end:
            raise ValueError("Reloj de snapshot fuera de intervalo")
        if (
            s.next_open.second
            or s.next_open.microsecond
            or not s.next_open <= s.clock.now < s.next_open + MINUTE
        ):
            raise ValueError("Cursor de snapshot incompatible con el reloj")
        if s.fact_cursor < 0 or s.event_count < 0:
            raise ValueError("Cursor o contador inválido")
        if len(s.event_digest) != 64:
            raise ValueError("Huella inválida")
        bytes.fromhex(s.event_digest)
        state = s.coordinator.strategy_state
        if state is not None:
            if not isinstance(state, AltBStrategyState):
                raise TypeError("Replay requiere AltBStrategyState")
            if state.last_event_at is not None and state.last_event_at > s.clock.now:
                raise ValueError("Estado futuro respecto al reloj")
            if (
                state.pending_entry is not None
                and state.pending_entry.action not in s.coordinator.pending_actions
            ):
                raise ValueError("Snapshot perdió una acción pendiente")
            if any(a.not_before <= s.clock.now for a in s.coordinator.pending_actions):
                raise ValueError("Snapshot contiene acciones vencidas")
            self.coordinator.on_entry_control(
                s.coordinator, state.allow_entries, state.last_event_at or s.clock.now
            )

    def _record(self, kind: str, payload: object) -> None:
        s = self._snapshot
        digest = sha256(
            bytes.fromhex(s.event_digest) + encoded((s.clock.now, kind, payload))
        ).hexdigest()
        self._snapshot = replace(s, event_digest=digest, event_count=s.event_count + 1)

    def _emit(self, emission: ReplayEmission) -> None:
        self._record("emission", emission)
        self._snapshot = replace(self._snapshot, emissions=self._snapshot.emissions + (emission,))

    def _integrate(self, result: CoordinationResult, *, provenance: str | None = None) -> None:
        s = self._snapshot
        before = {a.action_id: a for a in s.coordinator.pending_actions}
        after = {a.action_id: a for a in result.state.pending_actions}
        self._snapshot = replace(s, coordinator=result.state)
        for signal in result.signals:
            self._emit(ReplayEmission(s.clock.now, "signal", signal))
        for action_id, action in after.items():
            if action_id not in before:
                self._emit(ReplayEmission(s.clock.now, "scheduled_action", action))
        for action_id, action in before.items():
            if action_id not in after:
                self._emit(ReplayEmission(s.clock.now, "cancelled_action", action))
        for intent in result.order_intents:
            if intent.created_at > s.clock.now:
                raise ValueError("El motor emitió una intención futura")
            self._emit(ReplayEmission(s.clock.now, "order_intent", intent, provenance))
        for request in result.cancel_requests:
            if request.created_at > s.clock.now:
                raise ValueError("El motor emitió una cancelación futura")
            self._emit(ReplayEmission(s.clock.now, "cancel_request", request, provenance))

    def _activate(self) -> None:
        s = self._snapshot
        state = s.coordinator.strategy_state
        warmup = (
            len(state.market.candles_1m)
            if isinstance(state, AltBStrategyState)
            and state.market.candles_1m
            and state.market.candles_1m[-1].close_time == s.request.start
            else 0
        )
        self._snapshot = replace(s, activated=True, stats=replace(s.stats, warmup_applied=warmup))
        self._record("evaluated_interval_start", s.request.allow_entries)
        self._integrate(
            self.coordinator.on_entry_control(s.coordinator, s.request.allow_entries, s.clock.now)
        )

    def _invalid_minute(self, opened: datetime, reason: str) -> None:
        s = self._snapshot
        intervals = s.stats.invalid_periods
        if intervals and intervals[-1].end == opened and intervals[-1].reason == reason:
            intervals = intervals[:-1] + (replace(intervals[-1], end=opened + MINUTE),)
        else:
            intervals += (InvalidPeriod(opened, opened + MINUTE, reason),)
        self._snapshot = replace(s, stats=replace(s.stats, invalid_periods=intervals))
        self._record("unavailable_minute", (opened, reason))
        for action in self._snapshot.coordinator.pending_actions:
            self._integrate(
                self.coordinator.invalidate_action(
                    self._snapshot.coordinator, action.action_id, self._snapshot.clock.now
                )
            )

    def _minute(self, candle: Candle | None) -> None:
        s = self._snapshot
        opened = s.next_open
        if candle is None:
            reason = (
                "pre_contract" if s.config.symbol == DEFAULT_SYMBOL and opened < LAUNCH else "gap"
            )
            self._invalid_minute(opened, reason)
        else:
            if candle.open_time != opened or candle.close_time != s.clock.now:
                raise ValueError("Vela no disponible en su instante causal")
            self._record("closed_candle", candle)
            warmup = opened < s.request.start
            stats = replace(
                s.stats,
                minutes_read=s.stats.minutes_read + 1,
                warmup_read=s.stats.warmup_read + int(warmup),
                evaluated_read=s.stats.evaluated_read + int(not warmup),
                first_read_open=s.stats.first_read_open or opened,
                effective_start=s.stats.effective_start or (None if warmup else opened),
                checkpoints=s.stats.checkpoints
                + int(not warmup and candle.close_time.minute % 15 == 0),
            )
            self._snapshot = replace(self._snapshot, stats=stats)
            self._integrate(self.coordinator.on_candle(self._snapshot.coordinator, candle))
            state = self._snapshot.coordinator.strategy_state
            if (
                not warmup
                and candle.close_time.minute % 15 == 0
                and isinstance(state, AltBStrategyState)
                and state.block_reason
            ):
                stats = self._snapshot.stats
                self._snapshot = replace(
                    self._snapshot,
                    stats=replace(
                        stats,
                        checkpoint_blocks=stats.checkpoint_blocks
                        + ((candle.close_time, state.block_reason),),
                    ),
                )
        self._snapshot = replace(self._snapshot, next_open=opened + MINUTE)

    def _fact(self, fact: ReplayFact) -> None:
        s = self._snapshot
        if fact.received_at != s.clock.now or fact.received_at < s.request.start:
            raise ValueError("Hecho fuera de su instante causal o del intervalo evaluado")
        self._record("explicit_fact", fact)
        payload = fact.payload
        if isinstance(payload, EntryControl):
            result = self.coordinator.on_entry_control(
                s.coordinator, payload.allow_entries, s.clock.now
            )
        elif isinstance(payload, Order):
            result = self.coordinator.on_order(s.coordinator, payload)
        else:
            assert isinstance(payload, FillEvidence)
            result = self.coordinator.on_fill(s.coordinator, payload.fill, payload.order)
        self._integrate(result, provenance=fact.provenance)
        self._snapshot = replace(self._snapshot, fact_cursor=s.fact_cursor + 1)

    def _due(self) -> None:
        s = self._snapshot
        due = self.coordinator.advance_time(s.coordinator, s.clock.now)
        self._snapshot = replace(s, coordinator=due.state)
        for action in due.due_actions:
            self._record("due_action", action)
            # Nunca se difiere una entrada hasta un Quote arbitrariamente tardío.
            evidence = self.quotes.at(action.symbol, s.clock.now)
            if evidence is not None and not isinstance(evidence, QuoteEvidence):
                raise TypeError("QuoteSource debe devolver QuoteEvidence explícita")
            if (
                evidence is None
                or evidence.quote.timestamp != action.not_before
                or evidence.quote.timestamp != s.clock.now
                or evidence.quote.symbol != action.symbol
            ):
                reason = (
                    "missing_quote" if evidence is None else "invalid_quote_timestamp_or_symbol"
                )
                self._emit(ReplayEmission(s.clock.now, "missing_quote", action, reason))
                self._integrate(
                    self.coordinator.invalidate_action(
                        self._snapshot.coordinator, action.action_id, s.clock.now
                    )
                )
                self._emit(ReplayEmission(s.clock.now, "cancelled_action", action))
                continue
            self._record("explicit_quote", evidence)
            self._integrate(
                self.coordinator.on_quote(self._snapshot.coordinator, evidence.quote),
                provenance=evidence.provenance,
            )
            self._integrate(
                self.coordinator.on_scheduled_action(
                    self._snapshot.coordinator, action, evidence.quote
                ),
                provenance=evidence.provenance,
            )

    def _read_minute(self) -> None:
        s = self._snapshot
        # Consulta indexada exclusivamente del minuto que ya ha cerrado.
        rows = self.history.read(s.config.symbol, s.next_open, s.next_open + MINUTE)
        try:
            candle = next(rows, None)
            if next(rows, None) is not None:
                raise ValueError("duplicate_candle")
        finally:
            rows.close()
        self._minute(candle)

    def run(self, until: datetime | None = None) -> ReplayReport:
        s = self._snapshot
        target = s.request.end if until is None else require_utc(until)
        if not s.clock.now <= target <= s.request.end:
            raise ValueError("until debe estar entre el reloj actual y end")
        if s.interruption is not None:
            return self.report()
        facts = self.facts.read(s.fact_cursor, target)
        try:
            fact = next(facts, None)
            last_key: tuple[datetime, int, int] | None = None
            while True:
                s = self._snapshot
                candidates = [s.next_open + MINUTE]
                if not s.activated:
                    candidates.append(s.request.start)
                if fact is not None:
                    if fact.received_at < s.clock.now or (
                        last_key is not None and fact.key <= last_key
                    ):
                        raise ValueError("Fuente de hechos fuera de orden")
                    candidates.append(fact.received_at)
                candidates.extend(a.not_before for a in s.coordinator.pending_actions)
                at = min(candidates)
                if at > target:
                    break
                self._snapshot = replace(s, clock=s.clock.advance(at))
                # En start termina primero el warmup y luego se habilita la evaluación.
                if at == s.request.start and not s.activated:
                    if at == s.next_open + MINUTE:
                        self._read_minute()
                    self._activate()
                # Control → Order → Fill (sequence explícita en cada clase).
                while fact is not None and fact.received_at == at:
                    self._fact(fact)
                    last_key = fact.key
                    fact = next(facts, None)
                if self._snapshot.next_open + MINUTE == at:
                    self._read_minute()
                # El cierre/gap y PAUSE del mismo instante preceden a la acción.
                self._due()
            self._snapshot = replace(self._snapshot, clock=self._snapshot.clock.advance(target))
        except (ValueError, TypeError, SQLiteError) as exc:
            self._snapshot = replace(self._snapshot, interruption=f"{type(exc).__name__}:{exc}")
            self._record("interruption", self._snapshot.interruption)
        return self.report()

    def report(self) -> ReplayReport:
        s = self._snapshot

        def count(kind: str) -> int:
            return sum(e.kind == kind for e in s.emissions)

        state = s.coordinator.strategy_state
        return ReplayReport(
            MARKET,
            s.config.symbol.value,
            "1m",
            s.request.start,
            s.request.end,
            s.clock.now,
            s.stats,
            count("signal"),
            count("scheduled_action"),
            count("cancelled_action"),
            count("missing_quote"),
            sum(
                e.kind == "order_intent" and e.provenance == "SYNTHETIC_FIXTURE"
                for e in s.emissions
            ),
            count("order_intent"),
            count("cancel_request"),
            s.coordinator,
            state.block_reason if isinstance(state, AltBStrategyState) else None,
            s.interruption,
            s.event_digest,
            s.event_count,
        )
