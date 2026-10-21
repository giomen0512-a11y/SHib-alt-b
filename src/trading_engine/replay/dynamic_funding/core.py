"""Asignador puro y acotado: evidencia de exposición, nunca otro motor de PnL."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from trading_engine.models._validation import require_type
from trading_engine.models.ids import RoundId
from trading_engine.models.values import Symbol
from trading_engine.replay.financial_ledger.models import LedgerState
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance.core import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import (
    CoverageEvidence,
    FinancialEvent,
    FundingEvidence,
    Origin,
    Reference,
)
from trading_engine.replay.historical_validation.funding_source import (
    HistoricalFundingRateEvent,
    HistoricalFundingSource,
)
from trading_engine.replay.historical_validation.registry import digest
from trading_engine.replay.ports import (
    EntryControl,
    FactSource,
    FillEvidence,
    FixtureFacts,
    ReplayFact,
)
from trading_engine.replay.simulated_execution.core import SimulatedExecution
from trading_engine.replay.simulated_execution.state import ExecutionState

from .contracts import (
    DynamicFundingPolicy,
    FixtureCalendarSource,
    FixtureFinalitySource,
)
from .models import (
    DecisionStatus,
    FinalizationProof,
    FundingDecision,
    FundingReport,
    FundingState,
)

ZERO = Decimal("0")


class FundingConflict(ValueError):
    """Contradicción de una autoridad fixture; no es un conflicto artificial del ledger."""


@dataclass(frozen=True)
class FundingContext:
    at: datetime
    start: datetime
    end: datetime
    ledger: LedgerState
    fact_identity: str
    next_minute: datetime
    simulator: SimulatedExecution | None = None
    simulation: ExecutionState | None = None


@dataclass(frozen=True)
class DynamicFunding:
    source: HistoricalFundingSource
    policy: DynamicFundingPolicy
    finality: FixtureFinalitySource | None = None
    calendars: FixtureCalendarSource | None = None
    _session: object = field(default_factory=object, init=False, repr=False, compare=False)
    _prepared: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_type(self.source, HistoricalFundingSource, "funding_source")
        require_type(self.policy, DynamicFundingPolicy, "funding_policy")
        if self.finality is not None:
            require_type(self.finality, FixtureFinalitySource, "finality_source")
        if self.calendars is not None:
            require_type(self.calendars, FixtureCalendarSource, "calendar_source")
        object.__setattr__(self, "_prepared", self.source.history_session)
        self.check()

    def check(self) -> None:
        self.source.check()
        if self.source.history_session is not self._prepared:
            raise ValueError("DYNAMIC_FUNDING_PREPARATION_CHANGED")

    @property
    def session(self) -> object:
        self.check()
        return self._session

    @property
    def identity(self) -> str:
        self.check()
        return "dynamic-funding-v1:" + digest(
            (
                self.source.identity,
                self.policy,
                None if self.finality is None else self.finality.identity,
                None if self.calendars is None else self.calendars.identity,
            )
        )

    def initial(
        self,
        start: datetime,
        end: datetime,
        facts: FactSource,
        simulator: SimulatedExecution | None,
        ledger_identity: str,
    ) -> FundingState:
        self.check()
        if (
            self.source.history.start != start
            or end > self.source.history.cutoff
            or end - start > timedelta(minutes=self.policy.max_window_minutes)
        ):
            raise ValueError("DYNAMIC_FUNDING_WINDOW_INVALID")
        if self.finality is not None:
            if not isinstance(facts, FixtureFacts):
                raise ValueError("DYNAMIC_FINALITY_REQUIRES_CLOSED_FIXTURE_FACTS")
            if any(
                e.fact_identity != facts.identity or e.received_at < start
                for e in self.finality.events
            ):
                raise ValueError("DYNAMIC_FINALITY_SOURCE_BINDING_INVALID")
        if simulator is not None and type(simulator) is not SimulatedExecution:
            raise ValueError("DYNAMIC_FINALITY_SIMULATOR_CONTRACT_REQUIRED")
        if simulator is not None and self.finality is not None:
            raise ValueError("DYNAMIC_FINALITY_HAS_TWO_AUTHORITIES")
        if simulator is not None and (
            not isinstance(facts, FixtureFacts)
            or any(not isinstance(f.payload, EntryControl) for f in facts.facts)
        ):
            raise ValueError("DYNAMIC_FUNDING_NO_EXTERNAL_EXECUTION_WITH_SIMULATOR")
        if self.calendars is not None and any(
            c.funding_economic_identity != self.source.history.evidence.economic_identity
            or not start <= c.start < c.end <= end
            for c in self.calendars.events
        ):
            raise ValueError("DYNAMIC_CALENDAR_SOURCE_BINDING_INVALID")
        binding = (
            start,
            end,
            facts.identity,
            None if simulator is None else simulator.identity,
            ledger_identity,
        )
        economic = digest(
            (
                self.source.history.evidence.economic_identity,
                self.policy,
                None if self.finality is None else self.finality.identity,
                None if self.calendars is None else self.calendars.identity,
                binding,
            )
        )
        return FundingState("funding-replay-state-v1:" + digest((self.identity, binding)), economic)

    def next_at(
        self, state: FundingState, now: datetime, simulator: SimulatedExecution | None
    ) -> datetime | None:
        self.check()
        candidates = []
        next_rate = self.source.next_received_at(state.cursor)
        if next_rate is not None:
            candidates.append(next_rate)
        if self.finality is not None and state.finality_cursor < len(self.finality.events):
            candidates.append(self.finality.events[state.finality_cursor].received_at)
        if self.calendars is not None and state.calendar_cursor < len(self.calendars.events):
            candidates.append(self.calendars.events[state.calendar_cursor].received_at)
        inventory = self.source.history.evidence.inventory
        if state.inventory is None and inventory is not None:
            candidates.append(
                inventory.market_available_at
                + timedelta(milliseconds=self.source.policy.latency_ms)
            )
        if simulator is not None:
            candidates.extend(
                at
                for event in state.pending
                if (
                    at := event.rate.market_effective_at
                    + timedelta(milliseconds=simulator.policy.delivery_latency_ms)
                )
                > now
            )
        self.check()
        return min(candidates) if candidates else None

    def validate_fact(self, state: FundingState, fact: ReplayFact, ledger: LedgerState) -> None:
        self.check()
        if not isinstance(fact.payload, FillEvidence):
            return
        fill = fact.payload.fill
        if fill.fill_id in ledger.processed_fill_ids:
            return  # El ledger conserva su comprobación de contenido para duplicados.
        if any(
            d.finalization is not None
            and fill.timestamp <= d.finalization.through
            and fill.fill_id not in d.finalization.fill_ids
            for d in state.journal
        ):
            raise FundingConflict("DYNAMIC_FUNDING_LATE_FILL_CONTRADICTS_FINALITY")

    def _proof(
        self, state: FundingState, event: HistoricalFundingRateEvent, context: FundingContext
    ) -> FinalizationProof | None:
        self.check()
        at = event.rate.market_effective_at
        if context.simulator is not None:
            simulation = context.simulation
            if simulation is None:
                raise FundingConflict("DYNAMIC_FUNDING_SIMULATION_STATE_MISSING")
            available = at + timedelta(milliseconds=context.simulator.policy.delivery_latency_ms)
            if context.at < available or context.next_minute <= at:
                return None
            expected = tuple(
                sorted(
                    (
                        f.payload.fill.fill_id
                        for f in simulation.facts
                        if isinstance(f.payload, FillEvidence) and f.payload.fill.timestamp <= at
                    ),
                    key=lambda i: i.int,
                )
            )
            actual = tuple(
                sorted(
                    (e.fill_id for e in context.ledger.entries if e.executed_at <= at),
                    key=lambda i: i.int,
                )
            )
            if expected != actual:
                raise FundingConflict("DYNAMIC_SIMULATED_FINALITY_DELIVERIES_INCOMPLETE")
            authority = context.simulator.identity
            proof = FinalizationProof(
                "simulated-exposure-proof-v1:" + digest((authority, at, available, expected)),
                at,
                available,
                expected,
                authority,
                "SIMULATOR_GENERATION_AND_DELIVERY_BOUND",
            )
        else:
            candidates = [e for e in state.finalities if e.through >= at]
            if not candidates:
                return None
            declaration = candidates[0]
            actual = tuple(
                sorted(
                    (
                        e.fill_id
                        for e in context.ledger.entries
                        if e.executed_at <= declaration.through
                    ),
                    key=lambda i: i.int,
                )
            )
            if set(actual) - set(declaration.fill_ids):
                raise FundingConflict("DYNAMIC_FINALITY_HAS_UNDECLARED_FILLS")
            if actual != declaration.fill_ids:
                return None
            received = max(
                (declaration.received_at,)
                + tuple(
                    e.received_at
                    for e in context.ledger.entries
                    if e.fill_id in declaration.fill_ids
                )
            )
            proof = FinalizationProof(
                declaration.identity,
                declaration.through,
                received,
                actual,
                declaration.fact_identity,
                "DECLARED_CLOSED_FIXTURE_EXECUTIONS",
            )
        self.check()
        return proof

    def _decision(
        self,
        state: FundingState,
        event: HistoricalFundingRateEvent,
        context: FundingContext,
    ) -> FundingDecision:
        self.check()
        rate = event.rate
        proof = self._proof(state, event, context)
        status: DecisionStatus = "PENDING_EXPOSURE"
        reason = "EXPOSURE_FINALITY_NOT_AVAILABLE"
        round_id: RoundId | None = None
        quantity: Decimal | None = None
        payment = None
        entries = tuple(
            e for e in context.ledger.entries if e.executed_at <= rate.market_effective_at
        )
        fill_ids = tuple(sorted((e.fill_id for e in entries), key=lambda i: i.int))
        reference = (
            rate.mark_price
            if self.policy.reference_semantics == "FIXTURE_MARK_AT_FUNDING_TIME"
            else None
        )
        if proof is not None:
            if any(e.executed_at == rate.market_effective_at for e in entries):
                status, reason = "BLOCKED", "FUNDING_FILL_PRIORITY_NOT_DEMONSTRATED"
            else:
                self.check()
                with localcontext(ledger_context()):
                    exposed = [
                        (
                            rid,
                            FinancialPerformance._exposure(
                                context.ledger, rid, rate.market_effective_at
                            ),
                        )
                        for rid in sorted({e.round_id for e in entries}, key=lambda i: i.int)
                    ]
                if any(q < ZERO for _, q in exposed) or sum(q > ZERO for _, q in exposed) > 1:
                    status, reason = "BLOCKED", "LONG_ONE_WAY_EXPOSURE_NOT_UNIQUE"
                else:
                    active = [(rid, q) for rid, q in exposed if q > ZERO]
                    quantity = active[0][1] if active else ZERO
                    round_id = active[0][0] if active else None
                    if quantity == ZERO:
                        status, reason = "NO_EXPOSURE", "FINALIZED_ZERO_EXPOSURE"
                    elif reference is None:
                        status, reason = "FINALIZED_EXPOSURE", "REFERENCE_NOT_AVAILABLE"
                    else:
                        self.check()
                        binding = (
                            state.economic_identity,
                            event.event_id,
                            round_id,
                            fill_ids,
                            quantity,
                            reference,
                            rate.rate,
                            proof.identity,
                        )
                        assert round_id is not None
                        payload = FundingEvidence(
                            "dynamic-funding-payment-v1:" + digest(binding),
                            Symbol("1000SHIBUSDC"),
                            rate.market_effective_at,
                            round_id,
                            quantity,
                            Reference(
                                "dynamic-funding-authorized-synthetic-fixture",
                                state.economic_identity,
                                Origin.MODELED,
                                self.policy.assumptions + "; " + proof.kind,
                            ),
                            rate=rate.rate,
                            reference_price=reference,
                            reference_price_at=rate.market_effective_at,
                            positive_rate_pays=True,
                        )
                        payment = FinancialEvent(context.at, len(state.events), payload)
                        self.check()
                        status, reason = "ASSIGNED", "FINALIZED_HISTORICAL_LONG_EXPOSURE"
        if reference is None and status == "NO_EXPOSURE":
            reason = "REFERENCE_NOT_AVAILABLE"
        identity = "funding-decision-v1:" + digest(
            (
                state.economic_identity,
                event.event_id,
                context.at,
                status,
                reason,
                round_id,
                fill_ids,
                quantity,
                reference,
                None if proof is None else proof.identity,
            )
        )
        result = FundingDecision(
            identity,
            event,
            context.at,
            status,
            reason,
            state.economic_identity,
            self.policy.identity,
            self.source.history.evidence.inventory_identity,
            round_id,
            fill_ids,
            quantity,
            reference,
            None if reference is None else rate.market_effective_at,
            proof,
            payment,
        )
        self.check()
        return result

    def _append(self, state: FundingState, decision: FundingDecision) -> FundingState:
        prior = next(
            (
                d
                for d in reversed(state.journal)
                if d.rate_event.event_id == decision.rate_event.event_id
            ),
            None,
        )
        if (
            prior is not None
            and replace(decision, identity=prior.identity, received_at=prior.received_at) == prior
        ):
            return state
        if len(state.journal) >= self.policy.max_journal_entries:
            raise ValueError("DYNAMIC_FUNDING_JOURNAL_LIMIT")
        return replace(state, journal=state.journal + (decision,))

    def _calendar_check(self, state: FundingState) -> None:
        for calendar in state.calendars:
            actual = {
                d.rate_event.rate.market_effective_at
                for d in state.latest
                if calendar.start <= d.rate_event.rate.market_effective_at <= calendar.end
            }
            if actual - set(calendar.expected_timestamps):
                raise FundingConflict("DYNAMIC_FUNDING_RATE_CONTRADICTS_CLOSED_CALENDAR")
            for other in state.calendars:
                start, end = max(calendar.start, other.start), min(calendar.end, other.end)
                if start <= end and (
                    tuple(t for t in calendar.expected_timestamps if start <= t <= end)
                    != tuple(t for t in other.expected_timestamps if start <= t <= end)
                ):
                    raise FundingConflict("DYNAMIC_FUNDING_CALENDAR_CONTRADICTION")

    def _coverage(self, state: FundingState, context: FundingContext) -> FundingState:
        publication = state.inventory
        if publication is None:
            return state
        inventory = publication.inventory
        for calendar in state.calendars:
            if any(
                isinstance(e.payload, CoverageEvidence)
                and (e.payload.start, e.payload.end) == (calendar.start, calendar.end)
                for e in state.coverage
            ):
                continue
            if not inventory.start <= calendar.start < calendar.end <= inventory.end:
                continue
            body = tuple(
                t for t in inventory.expected_timestamps if calendar.start <= t <= calendar.end
            )
            expected_body = tuple(t for t in calendar.expected_timestamps if t < inventory.end)
            if body != expected_body:
                raise FundingConflict("DYNAMIC_FUNDING_INVENTORY_CALENDAR_CONTRADICTION")
            latest = tuple(
                d
                for d in state.latest
                if calendar.start <= d.rate_event.rate.market_effective_at <= calendar.end
            )
            if {d.rate_event.rate.market_effective_at for d in latest} != set(
                calendar.expected_timestamps
            ):
                continue
            if any(
                d.status not in {"ASSIGNED", "NO_EXPOSURE"}
                or d.finalization is None
                or d.reference_price is None
                for d in latest
            ):
                continue
            ids = tuple(d.payment.payload.event_id for d in latest if d.payment is not None)
            self.check()
            evidence = CoverageEvidence(
                "dynamic-funding-coverage-v1:"
                + digest(
                    (
                        state.economic_identity,
                        calendar.economic_key,
                        publication.inventory_identity,
                        tuple(sorted(ids)),
                        tuple(d.identity for d in latest),
                    )
                ),
                calendar.end,
                calendar.start,
                calendar.end,
                "FUNDING",
                ids,
                Reference(
                    "dynamic-funding-economic-fixture-inventory",
                    state.economic_identity,
                    Origin.MODELED,
                    self.policy.assumptions,
                ),
            )
            event = FinancialEvent(context.at, len(state.events), evidence)
            self.check()
            state = replace(state, coverage=state.coverage + (event,))
        return state

    def propose(
        self, state: FundingState, context: FundingContext
    ) -> tuple[FundingState, tuple[FinancialEvent, ...]]:
        self.check()
        binding = (
            context.start,
            context.end,
            context.fact_identity,
            None if context.simulator is None else context.simulator.identity,
            context.ledger.identity,
        )
        if state.identity != "funding-replay-state-v1:" + digest((self.identity, binding)):
            raise FundingConflict("DYNAMIC_FUNDING_STATE_BINDING_CHANGED")
        if not context.start <= context.at <= context.end:
            raise FundingConflict("DYNAMIC_FUNDING_CONTEXT_OUTSIDE_REPLAY")
        if context.ledger.conflicts or len(context.ledger.entries) > self.policy.max_fills:
            raise FundingConflict("DYNAMIC_FUNDING_LEDGER_BLOCKED_OR_LIMITED")
        before_ids = {e.payload.event_id for e in state.events}
        for event in self.source.read(state.cursor, context.at):
            self.check()
            if event.sequence != state.cursor:
                raise FundingConflict("DYNAMIC_FUNDING_CURSOR_CHANGED")
            if state.cursor >= self.policy.max_rates:
                raise ValueError("DYNAMIC_FUNDING_RATE_LIMIT")
            if not context.start <= event.rate.market_effective_at <= context.end:
                raise FundingConflict("DYNAMIC_FUNDING_RATE_OUTSIDE_REPLAY")
            prior = next((d for d in state.latest if d.rate_event.event_id == event.event_id), None)
            if prior is not None and prior.rate_event.rate != event.rate:
                raise FundingConflict("DYNAMIC_FUNDING_RATE_CONTRADICTION")
            state = replace(
                state,
                cursor=state.cursor + 1,
                pending=state.pending + (() if prior is not None else (event,)),
            )
        if self.finality is not None:
            for declaration in self.finality.events[state.finality_cursor :]:
                if declaration.received_at > context.at:
                    break
                known = {
                    e.fill_id
                    for e in context.ledger.entries
                    if e.executed_at <= declaration.through
                }
                if known - set(declaration.fill_ids):
                    raise FundingConflict("DYNAMIC_FINALITY_HAS_UNDECLARED_FILLS")
                for previous in state.finalities:
                    lower, upper = sorted((previous, declaration), key=lambda e: e.through)
                    if set(lower.fill_ids) - set(upper.fill_ids) or (
                        lower.through == upper.through and lower.fill_ids != upper.fill_ids
                    ):
                        raise FundingConflict("DYNAMIC_FINALITY_MANIFEST_CONTRADICTION")
                state = replace(
                    state,
                    finality_cursor=state.finality_cursor + 1,
                    finalities=state.finalities + (declaration,),
                )
        if self.calendars is not None:
            for calendar in self.calendars.events[state.calendar_cursor :]:
                if calendar.received_at > context.at:
                    break
                state = replace(state, calendar_cursor=state.calendar_cursor + 1)
                if not any(c.economic_key == calendar.economic_key for c in state.calendars):
                    state = replace(state, calendars=state.calendars + (calendar,))
        publication = self.source.inventory_at(context.at)
        if publication is not None:
            if state.inventory is not None and state.inventory != publication:
                raise FundingConflict("DYNAMIC_FUNDING_INVENTORY_CHANGED")
            state = replace(state, inventory=publication)
        still_pending = []
        for event in state.pending:
            decision = self._decision(state, event, context)
            state = self._append(state, decision)
            if decision.status == "PENDING_EXPOSURE":
                still_pending.append(event)
        state = replace(state, pending=tuple(still_pending))
        self._calendar_check(state)
        state = self._coverage(state, context)
        self.check()
        return state, tuple(e for e in state.events if e.payload.event_id not in before_ids)

    def report(self, state: FundingState) -> FundingReport:
        self.check()
        reasons = tuple(
            sorted({d.reason for d in state.latest if d.status not in {"ASSIGNED", "NO_EXPOSURE"}})
        )
        status = (
            "BLOCKED"
            if any(d.status == "BLOCKED" for d in state.latest)
            else "PENDING"
            if reasons or state.pending
            else "COVERAGE_PUBLISHED"
            if state.coverage
            else "NOT_AVAILABLE"
        )
        result = FundingReport(
            state.identity,
            state.economic_identity,
            state.cursor,
            state.latest,
            state.journal,
            state.coverage,
            status,
            reasons,
        )
        self.check()
        return result
