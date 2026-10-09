"""Runner causal offline: un único AltBEngine/StrategyCoordinator, sin ejecución."""

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from sqlite3 import Error as SQLiteError

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, MARKET
from trading_engine.models.ids import RoundId
from trading_engine.models.market import Candle
from trading_engine.models.orders import Order
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinationResult
from trading_engine.replay.clock import SimulatedClock
from trading_engine.replay.encoding import encoded
from trading_engine.replay.execution_model.quotes import ModeledQuotes
from trading_engine.replay.financial_ledger.core import FinancialLedger
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_performance.core import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import FinancialEvent, MarkEvidence
from trading_engine.replay.financial_performance.models import FinancialObservation
from trading_engine.replay.history import SQLiteHistory
from trading_engine.replay.isolated_margin_risk.core import IsolatedMarginRisk
from trading_engine.replay.isolated_margin_risk.evidence import RiskEvent
from trading_engine.replay.isolated_margin_risk.models import RiskObservation
from trading_engine.replay.models import (
    InvalidPeriod,
    ReplayEmission,
    ReplayReport,
    ReplayRequest,
    ReplaySnapshot,
)
from trading_engine.replay.ports import (
    EntryControl,
    ExecutionQuoteSource,
    FactSource,
    FillEvidence,
    FixtureFacts,
    NoQuotes,
    QuoteEvidence,
    QuoteSource,
    ReplayFact,
)
from trading_engine.replay.simulated_execution.core import SimulatedExecution
from trading_engine.replay.simulated_execution.state import ExecutionState
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
        simulator: SimulatedExecution | None = None,
        ledger: FinancialLedger | None = None,
        performance: FinancialPerformance | None = None,
        risk: IsolatedMarginRisk | None = None,
    ) -> None:
        config = AltBConfig() if config is None else config
        quotes = NoQuotes() if quotes is None else quotes
        facts = FixtureFacts() if facts is None else facts
        self.history = history
        self.quotes = quotes
        self.facts = facts
        self.simulator = simulator
        self.ledger = ledger
        self.performance = performance
        self.risk = risk
        if performance is not None and ledger is None:
            raise ValueError("Performance opt-in requiere FinancialLedger explícito")
        if risk is not None and performance is None:
            raise ValueError("Riesgo opt-in requiere FinancialPerformance explícito")
        self._risk_events = (
            ()
            if risk is None
            else tuple(
                sorted(
                    risk.source.read(0, request.end),
                    key=lambda e: (e.stamp.received_at, e.stamp.sequence),
                )
            )
        )
        if len({e.stamp.sequence for e in self._risk_events}) != len(self._risk_events):
            raise ValueError("Fuente de riesgo repite secuencia")
        if any(not request.start <= e.stamp.received_at <= request.end for e in self._risk_events):
            raise ValueError("Fuente de riesgo fuera de intervalo/límite de lectura")
        self._financial_events = (
            ()
            if performance is None
            else tuple(
                sorted(
                    (
                        (kind, event)
                        for kind, source in (
                            ("costs", performance.source),
                            ("marks", performance.marks),
                        )
                        for event in source.read(0, request.end)
                    ),
                    key=lambda pair: (pair[1].received_at, pair[0], pair[1].sequence),
                )
            )
        )
        if performance is not None:
            keys = [(kind, event.sequence) for kind, event in self._financial_events]
            if len(set(keys)) != len(keys):
                raise ValueError("Fuente financiera repite secuencia")
            for kind, event in self._financial_events:
                if event.received_at < request.start:
                    raise ValueError("Fuente financiera anterior al intervalo evaluado")
                if (kind == "marks") != isinstance(event.payload, MarkEvidence):
                    raise ValueError("Mark debe proceder exclusivamente de MarkPriceSource")
                if event.received_at > request.end:
                    raise ValueError("Fuente financiera violó el límite de lectura")
        if simulator is not None:
            if simulator.quotes is not quotes:
                raise ValueError("Simulador y runner requieren la misma fuente QuoteSource")
            if isinstance(quotes, ModeledQuotes) and quotes.policy != simulator.policy.pricing:
                raise ValueError("Política de precios del simulador incompatible")
        self.coordinator = StrategyCoordinator(AltBEngine(config))
        execution_identity = None
        if isinstance(quotes, ExecutionQuoteSource):
            if quotes.history_session is not history.session:
                raise ValueError("Fuente modelada debe usar la misma sesión SQLite del runner")
            execution_identity = quotes.execution_identity
        start = request.warmup_start(config)
        initial = ReplaySnapshot(
            request,
            config,
            history.session,
            quotes.identity,
            facts.identity,
            SimulatedClock(start),
            start,
            execution_identity=execution_identity,
            simulation_identity=None if simulator is None else simulator.identity,
            simulation=None if simulator is None else simulator.initial(),
            ledger_identity=None if ledger is None else ledger.identity,
            ledger=None if ledger is None else ledger.initial(),
            performance_identity=None if performance is None else performance.identity,
            performance=None
            if performance is None
            else performance.initial(request.start, request.end),
            performance_cursor=None if performance is None else 0,
            performance_pending=None if performance is None else self._financial_events,
            risk_identity=None if risk is None else risk.identity,
            risk=None if risk is None else risk.initial(request.start, request.end),
            risk_cursor=None if risk is None else 0,
            risk_pending=None if risk is None else self._risk_events,
        )
        self._snapshot = initial if snapshot is None else snapshot
        if snapshot is not None:
            if (
                snapshot.request,
                snapshot.config,
                snapshot.history_session,
                snapshot.quote_identity,
                snapshot.fact_identity,
                snapshot.execution_identity,
                snapshot.simulation_identity,
                snapshot.ledger_identity,
                snapshot.performance_identity,
                snapshot.risk_identity,
            ) != (
                request,
                config,
                history.session,
                quotes.identity,
                facts.identity,
                execution_identity,
                None if simulator is None else simulator.identity,
                None if ledger is None else ledger.identity,
                None if performance is None else performance.identity,
                None if risk is None else risk.identity,
            ):
                raise ValueError(
                    "Reanudación requiere la misma sesión SQLite/configuración/fuentes"
                )
            self._validate_snapshot()
        else:
            self._record("session", (request, config))
            if execution_identity is not None:
                self._record("execution_model", execution_identity)
            if simulator is not None:
                self._record("simulated_execution", simulator.identity)
            if ledger is not None:
                self._record("financial_ledger", ledger.identity)
            if performance is not None:
                self._record("financial_performance", performance.identity)
            if risk is not None:
                self._record("isolated_margin_risk", risk.identity)
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
        if self.simulator is not None:
            if s.simulation is None:
                raise ValueError("Snapshot perdió el estado del simulador")
            self.simulator.validate(s.simulation, s.clock.now)
        elif s.simulation is not None:
            raise ValueError("Snapshot simulado requiere simulador")
        if self.ledger is not None:
            if s.ledger is None:
                raise ValueError("Snapshot perdió estado financiero")
            self.ledger.validate(s.ledger, s.clock.now)
            if (
                self.ledger.reconcile_closed(s.ledger, self._closed_rounds(), at=s.clock.now)
                != s.ledger
            ):
                raise ValueError("Snapshot perdió conciliación de cierre financiero")
        elif s.ledger is not None:
            raise ValueError("Snapshot financiero requiere ledger")
        if self.performance is not None:
            if s.performance is None or s.performance_cursor is None or s.ledger is None:
                raise ValueError("Snapshot perdió estado de performance")
            if not 0 <= s.performance_cursor <= len(self._financial_events):
                raise ValueError("Cursor financiero inválido")
            if s.performance_pending != self._financial_events[s.performance_cursor :]:
                raise ValueError("Snapshot perdió eventos financieros pendientes")
            delivered = self._financial_events[: s.performance_cursor]
            if any(event.received_at > s.clock.now for _, event in delivered):
                raise ValueError("Cursor financiero anticipó evidencia futura")
            if s.interruption is None and any(
                event.received_at <= s.clock.now for _, event in s.performance_pending
            ):
                raise ValueError("Snapshot perdió una entrega financiera vencida")
            expected_events = s.performance.events
            accepted: list[FinancialEvent] = []
            for _, event in delivered:
                if not any(e.payload.event_id == event.payload.event_id for e in accepted):
                    accepted.append(event)
            if not s.performance.conflicts and tuple(accepted) != expected_events:
                raise ValueError("Cursor financiero no coincide con eventos reconocidos")
            if s.activated:
                self.performance.validate(s.performance, s.clock.now, s.ledger)
            elif s.performance != self.performance.initial(s.request.start, s.request.end):
                raise ValueError("Snapshot financiero alterado antes del intervalo")
        elif any(
            v is not None for v in (s.performance, s.performance_cursor, s.performance_pending)
        ):
            raise ValueError("Snapshot ampliado requiere componente performance")
        if self.risk is not None:
            if s.risk is None or s.risk_cursor is None or s.performance is None:
                raise ValueError("Snapshot perdió estado de riesgo")
            if type(s.risk_cursor) is not int or not 0 <= s.risk_cursor <= len(self._risk_events):
                raise ValueError("Cursor de riesgo inválido")
            if s.risk_pending != self._risk_events[s.risk_cursor :]:
                raise ValueError("Snapshot perdió evidencia de riesgo pendiente")
            delivered_risk = self._risk_events[: s.risk_cursor]
            if any(e.stamp.received_at > s.clock.now for e in delivered_risk):
                raise ValueError("Cursor de riesgo anticipó eventos futuros")
            if s.interruption is None and any(
                e.stamp.received_at <= s.clock.now for e in s.risk_pending
            ):
                raise ValueError("Snapshot perdió entregas de riesgo vencidas")
            accepted_risk: list[RiskEvent] = []
            for event_risk in delivered_risk:
                if not any(e.stamp.event_id == event_risk.stamp.event_id for e in accepted_risk):
                    accepted_risk.append(event_risk)
            if not s.risk.conflicts and tuple(accepted_risk) != s.risk.events:
                raise ValueError("Cursor de riesgo no coincide con eventos reconocidos")
            assert self.performance is not None
            if s.activated:
                processed_risk = self.risk.validate(
                    s.risk, s.clock.now, self.performance, s.performance
                )
                if processed_risk != s.risk_cursor:
                    raise ValueError("Cursor de riesgo adulterado; reconstrucción distinta")
                # El contexto final debe consumir todo el prefijo financiero disponible.
                if not s.risk.conflicts and s.risk.context != self.risk.context_from(
                    self.performance, s.performance, s.risk.last_at or s.request.start
                ):
                    raise ValueError("Riesgo perdió su última transición financiera")
            elif s.risk != self.risk.initial(s.request.start, s.request.end):
                raise ValueError("Riesgo alterado antes de activación")
        elif any(v is not None for v in (s.risk, s.risk_cursor, s.risk_pending)):
            raise ValueError("Snapshot de riesgo requiere componente explícito")
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
            if self.simulator is not None:
                assert self._snapshot.simulation is not None
                self._simulation_update(
                    self.simulator.receive_intent(self._snapshot.simulation, intent, s.clock.now)
                )
        for request in result.cancel_requests:
            if request.created_at > s.clock.now:
                raise ValueError("El motor emitió una cancelación futura")
            self._emit(ReplayEmission(s.clock.now, "cancel_request", request, provenance))
            if self.simulator is not None:
                assert self._snapshot.simulation is not None
                self._simulation_update(
                    self.simulator.cancel(self._snapshot.simulation, request, s.clock.now)
                )
        if self.ledger is not None:
            assert self._snapshot.ledger is not None
            self._ledger_update(
                self.ledger.reconcile_closed(
                    self._snapshot.ledger, self._closed_rounds(), at=s.clock.now
                )
            )

    def _closed_rounds(self) -> tuple[RoundId, ...]:
        state = self._snapshot.coordinator.strategy_state
        if not isinstance(state, AltBStrategyState):
            return ()
        return tuple(
            r.base.intent.round_id
            for r in state.round_history
            if r.round_state.status(r.base) == "CLOSED"
        )

    def _ledger_update(self, state: LedgerState) -> None:
        before = self._snapshot.ledger
        assert before is not None
        if state != before:
            self._record(
                "ledger_transition",
                (
                    state.entries[len(before.entries) :],
                    state.journal[len(before.journal) :],
                    state.conflicts,
                ),
            )
            self._snapshot = replace(self._snapshot, ledger=state)
        if state.conflicts:
            raise ValueError("financial_ledger:" + state.conflicts[0].reason)

    def _simulation_update(self, state: ExecutionState) -> None:
        before = self._snapshot.simulation
        assert before is not None
        if state != before:
            self._record(
                "simulation_transition",
                (
                    state.audit[len(before.audit) :],
                    state.facts[len(before.facts) :],
                    state.sequence,
                    state.last_minute_at,
                ),
            )
            self._snapshot = replace(self._snapshot, simulation=state)

    def _financial_tick(self) -> None:
        if self.performance is None or not self._snapshot.activated:
            return
        s = self._snapshot
        assert s.performance is not None and s.ledger is not None
        assert s.performance_cursor is not None and s.performance_pending is not None
        state = self.performance.observe(s.performance, FinancialObservation(s.clock.now, s.ledger))
        cursor = s.performance_cursor
        pending = s.performance_pending
        while pending and pending[0][1].received_at == s.clock.now:
            state = self.performance.observe(
                state, FinancialObservation(s.clock.now, pending[0][1])
            )
            cursor += 1
            pending = pending[1:]
        state = self.performance.observe(state, FinancialObservation(s.clock.now, "VALUE"))
        if state != s.performance:
            self._record(
                "performance_transition",
                (state.journal[len(s.performance.journal) :], state.conflicts),
            )
        self._snapshot = replace(
            self._snapshot,
            performance=state,
            performance_cursor=cursor,
            performance_pending=pending,
        )
        if state.conflicts:
            raise ValueError("financial_performance:" + state.conflicts[0].reason)

    def _simulation_due(self) -> None:
        if self.simulator is None:
            return
        assert self._snapshot.simulation is not None
        while self.simulator.next_at(self._snapshot.simulation) == self._snapshot.clock.now:
            state, facts = self.simulator.advance(
                self._snapshot.simulation, self._snapshot.clock.now
            )
            self._simulation_update(state)
            for fact in facts:
                self._fact(fact, simulated=True)

    def _risk_tick(self) -> None:
        if self.risk is None or not self._snapshot.activated:
            return
        s = self._snapshot
        assert self.performance is not None and s.performance is not None and s.risk is not None
        assert s.risk_cursor is not None and s.risk_pending is not None
        state = self.risk.observe(
            s.risk,
            RiskObservation(
                s.clock.now, self.risk.context_from(self.performance, s.performance, s.clock.now)
            ),
        )
        cursor, pending = s.risk_cursor, s.risk_pending
        while pending and pending[0].stamp.received_at == s.clock.now and not state.conflicts:
            state = self.risk.observe(state, RiskObservation(s.clock.now, pending[0]))
            cursor += 1
            pending = pending[1:]
        if not state.conflicts:
            state = self.risk.observe(state, RiskObservation(s.clock.now, "VALUE"))
        if state != s.risk:
            self._record("risk_transition", (state.journal[len(s.risk.journal) :], state.conflicts))
        self._snapshot = replace(
            self._snapshot, risk=state, risk_cursor=cursor, risk_pending=pending
        )
        if state.conflicts:
            raise ValueError("isolated_margin_risk:" + state.conflicts[0].reason)

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
        if self.simulator is not None and self._snapshot.activated:
            assert self._snapshot.simulation is not None
            self._simulation_update(
                self.simulator.candle(self._snapshot.simulation, candle, s.clock.now)
            )

    def _fact(self, fact: ReplayFact, *, simulated: bool = False) -> None:
        s = self._snapshot
        if fact.received_at != s.clock.now or fact.received_at < s.request.start:
            raise ValueError("Hecho fuera de su instante causal o del intervalo evaluado")
        self._record("explicit_fact", fact)
        payload = fact.payload
        if self.simulator is not None and not simulated and not isinstance(payload, EntryControl):
            raise ValueError("No mezclar hechos externos de ejecución con ejecución modelada")
        if isinstance(payload, EntryControl):
            result = self.coordinator.on_entry_control(
                s.coordinator, payload.allow_entries, s.clock.now
            )
        elif isinstance(payload, Order):
            result = self.coordinator.on_order(s.coordinator, payload)
        else:
            assert isinstance(payload, FillEvidence)
            result = self.coordinator.on_fill(s.coordinator, payload.fill, payload.order)
            if self.ledger is not None:
                assert self._snapshot.ledger is not None
                self._ledger_update(
                    self.ledger.receive(self._snapshot.ledger, fact, at=s.clock.now)
                )
        self._integrate(result, provenance=fact.provenance)
        if not simulated:
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
            self._snapshot = replace(
                self._snapshot, price_evidence=self._snapshot.price_evidence + (evidence,)
            )
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
                if self.simulator is not None:
                    assert s.simulation is not None
                    next_execution = self.simulator.next_at(s.simulation)
                    if next_execution is not None:
                        candidates.append(next_execution)
                if s.performance_pending:
                    candidates.append(s.performance_pending[0][1].received_at)
                if s.risk_pending:
                    candidates.append(s.risk_pending[0].stamp.received_at)
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
                self._simulation_due()
                if self._snapshot.next_open + MINUTE == at:
                    self._read_minute()
                self._simulation_due()
                # El cierre/gap y PAUSE del mismo instante preceden a la acción.
                self._due()
                self._simulation_due()
                self._financial_tick()
                self._risk_tick()
            self._snapshot = replace(self._snapshot, clock=self._snapshot.clock.advance(target))
        except (ValueError, TypeError, SQLiteError) as exc:
            if (
                self.ledger is not None
                and self._snapshot.ledger is not None
                and not (
                    self._snapshot.performance is not None and self._snapshot.performance.conflicts
                )
                and not (self._snapshot.risk is not None and self._snapshot.risk.conflicts)
            ):
                state = self.ledger.block(
                    self._snapshot.ledger,
                    self._snapshot.clock.now,
                    f"replay_interruption:{type(exc).__name__}:{exc}",
                )
                if state != self._snapshot.ledger:
                    # Registrar bloqueo sin volver a lanzar desde _ledger_update.
                    self._record("ledger_conflict", state.conflicts)
                    self._snapshot = replace(self._snapshot, ledger=state)
            if (
                self.performance is not None
                and self._snapshot.activated
                and self._snapshot.performance is not None
                and self._snapshot.ledger is not None
                and self._snapshot.ledger.conflicts
            ):
                state_performance = self.performance.observe(
                    self._snapshot.performance,
                    FinancialObservation(self._snapshot.clock.now, self._snapshot.ledger),
                )
                if state_performance != self._snapshot.performance:
                    self._record("performance_conflict", state_performance.conflicts)
                    self._snapshot = replace(self._snapshot, performance=state_performance)
            if self.risk is not None and self._snapshot.activated:
                # Propagar interrupciones financieras sin alterar el prefijo válido.
                try:
                    self._risk_tick()
                except (ValueError, TypeError):
                    if self._snapshot.risk is None or not self._snapshot.risk.conflicts:
                        raise
            if (
                self.simulator is not None
                and self._snapshot.simulation is not None
                and not (self._snapshot.ledger is not None and self._snapshot.ledger.conflicts)
                and not (
                    self._snapshot.performance is not None and self._snapshot.performance.conflicts
                )
                and not (self._snapshot.risk is not None and self._snapshot.risk.conflicts)
            ):
                self._simulation_update(
                    self.simulator.conflict(
                        self._snapshot.simulation,
                        self._snapshot.clock.now,
                        f"{type(exc).__name__}:{exc}",
                    )
                )
            self._snapshot = replace(self._snapshot, interruption=f"{type(exc).__name__}:{exc}")
            self._record("interruption", self._snapshot.interruption)
        return self.report()

    def report(self) -> ReplayReport:
        s = self._snapshot

        def count(kind: str) -> int:
            return sum(e.kind == kind for e in s.emissions)

        state = s.coordinator.strategy_state
        financial = None
        availability = "FINANCIAL_RESULTS_NOT_AVAILABLE"
        if self.ledger is not None and s.ledger is not None:
            financial = self.ledger.report(s.ledger)
            if s.simulation is not None:
                financial = replace(financial, modeled_results=True)
            availability = (
                "PNL_BRUTO_REALIZADO_BLOQUEADO"
                if s.ledger.conflicts or s.interruption is not None
                else "PNL_BRUTO_REALIZADO_DISPONIBLE"
            )
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
            financial_results=availability,
            financial_ledger=financial,
            financial_performance=None
            if self.performance is None or s.performance is None or s.performance.ledger is None
            else self.performance.report(s.performance, s.clock.now),
            isolated_margin_risk=None
            if self.risk is None or s.risk is None or not s.activated
            else self.risk.report(
                s.risk,
                s.clock.now,
                None
                if self.performance is None or s.performance is None or s.performance.ledger is None
                else self.risk.context_from(self.performance, s.performance, s.clock.now),
            ),
            execution_identity=s.execution_identity,
            price_evidence=s.price_evidence,
            modeled_intents=sum(
                e.kind == "order_intent" and e.provenance == "MODELED_FROM_1M" for e in s.emissions
            ),
            simulation=s.simulation,
            execution_metrics=None
            if self.simulator is None or s.simulation is None
            else self.simulator.metrics(s.simulation),
        )
