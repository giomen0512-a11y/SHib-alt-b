"""Motor independiente de riesgo offline; sólo consume hechos confirmados."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import DecimalException, localcontext
from hashlib import sha256

from trading_engine.models._validation import require_type
from trading_engine.models.enums import Side
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance.core import FinancialPerformance
from trading_engine.replay.financial_performance.evidence import MarkEvidence
from trading_engine.replay.financial_performance.models import Metric, PerformanceState
from trading_engine.replay.isolated_margin_risk.evidence import (
    FixtureRiskSource,
    IsolatedMarginEvidence,
    MaintenanceBracketSchedule,
    MarginAdjustmentEvidence,
    RiskSource,
    RiskValuationEvidence,
)
from trading_engine.replay.isolated_margin_risk.models import (
    RiskConflict,
    RiskContext,
    RiskEvaluation,
    RiskObservation,
    RiskReport,
    RiskState,
)
from trading_engine.replay.isolated_margin_risk.policy import MarginAllocationPolicy
from trading_engine.replay.isolated_margin_risk.valuation import evaluate
from trading_engine.utils.time import require_utc


@dataclass(frozen=True)
class IsolatedMarginRisk:
    policy: MarginAllocationPolicy
    source: RiskSource = FixtureRiskSource()

    def __post_init__(self) -> None:
        require_type(self.policy, MarginAllocationPolicy, "policy")

    @property
    def identity(self) -> str:
        return (
            "offline-isolated-risk-v1:"
            + sha256(encoded((self.policy, self.source.identity))).hexdigest()
        )

    def initial(self, start: datetime, end: datetime) -> RiskState:
        require_utc(start)
        require_utc(end)
        if start >= end:
            raise ValueError("Intervalo de riesgo inválido")
        return RiskState(self.identity, self.policy, start, end)

    @staticmethod
    def context_from(
        performance: FinancialPerformance, state: PerformanceState, at: datetime
    ) -> RiskContext:
        if state.ledger is None:
            raise ValueError("Riesgo requiere exposición confirmada del ledger")
        marks = tuple(e.payload for e in state.events if isinstance(e.payload, MarkEvidence))
        financial = (
            performance.report(state, at).current
            if state.conflicts
            else performance.value(state, at)
        )
        return RiskContext(
            at,
            state.ledger,
            financial,
            marks[-1] if marks else None,
            performance.identity,
            len(state.journal),
        )

    def _time(self, state: RiskState, at: datetime) -> None:
        require_utc(at)
        if state.identity != self.identity or state.policy != self.policy:
            raise ValueError("Estado de otra política/fuente de riesgo")
        if not state.period_start <= at <= state.period_end:
            raise ValueError("Riesgo fuera del intervalo evaluado")
        if state.last_at is not None and at < state.last_at:
            raise ValueError("No modificar riesgo retrospectivamente")

    def observe(self, state: RiskState, observation: RiskObservation) -> RiskState:
        self._time(state, observation.at)
        if state.conflicts:
            return state
        try:
            with localcontext(ledger_context()):
                updated = self._observe(state, observation)
        except (ValueError, TypeError, DecimalException) as exc:
            return replace(
                state,
                conflicts=(
                    RiskConflict(observation.at, f"{type(exc).__name__}:{exc}", observation),
                ),
                last_at=observation.at,
            )
        if updated == state:
            return state
        return replace(updated, journal=state.journal + (observation,), last_at=observation.at)

    def _observe(self, state: RiskState, observation: RiskObservation) -> RiskState:
        at, payload = observation.at, observation.payload
        if isinstance(payload, RiskContext):
            if payload.at != at or payload.financial.at != at:
                raise ValueError("Contexto financiero fuera de instante causal")
            if payload.ledger.conflicts or any(
                m.status == "BLOCKED"
                for m in (
                    payload.financial.fees,
                    payload.financial.funding,
                    payload.financial.wallet,
                )
            ):
                raise ValueError("Componente financiero bloqueado")
            if (
                type(payload.performance_journal_cursor) is not int
                or payload.performance_journal_cursor < 0
            ):
                raise ValueError("Cursor de contexto inválido")
            if payload.mark is not None and (
                payload.mark.effective_at > at
                or payload.financial.mark_id != payload.mark.event_id
                or payload.mark.symbol != self.policy.symbol
            ):
                raise ValueError("Mark futuro o incompatible con performance")
            previous = state.context
            if previous is not None:
                if (
                    payload.performance_identity != previous.performance_identity
                    or payload.performance_journal_cursor < previous.performance_journal_cursor
                    or payload.ledger.identity != previous.ledger.identity
                    or payload.ledger.entries[: len(previous.ledger.entries)]
                    != previous.ledger.entries
                ):
                    raise ValueError("Contexto reescrito/de otra sesión")
                if payload.at == previous.at and payload != previous:
                    raise ValueError("Contextos financieros contradictorios en el mismo instante")
            if previous is None or payload.ledger != previous.ledger:
                FinancialLedger(payload.ledger.policy).validate(payload.ledger, at)
            return replace(state, context=payload)
        if isinstance(
            payload,
            (
                IsolatedMarginEvidence,
                MarginAdjustmentEvidence,
                MaintenanceBracketSchedule,
                RiskValuationEvidence,
            ),
        ):
            stamp = payload.stamp
            if stamp.received_at != at:
                raise ValueError("Evidencia fuera de su disponibilidad causal")
            prior = next((e for e in state.events if e.stamp.event_id == stamp.event_id), None)
            if prior is not None:
                # Retransmisión permite otra recepción/sequence, no otro hecho económico.
                comparable = replace(
                    payload,
                    stamp=replace(
                        stamp, received_at=prior.stamp.received_at, sequence=prior.stamp.sequence
                    ),
                )
                if comparable != prior:
                    raise ValueError("Identidad de evidencia contradictoria")
                return state
            if state.evaluations and state.evaluations[-1].at == at:
                raise ValueError("Instante ya valorado; no agregar hechos retrospectivamente")
            if isinstance(payload, MaintenanceBracketSchedule):
                if any(
                    isinstance(e, MaintenanceBracketSchedule)
                    and max(e.valid_from, payload.valid_from)
                    < min(e.valid_until, payload.valid_until)
                    for e in state.events
                ):
                    raise ValueError("Schedules con vigencias solapadas")
            else:
                if state.context is None:
                    raise ValueError("Evidencia de posición requiere contexto confirmado")
                entries = tuple(
                    e for e in state.context.ledger.entries if e.round_id == payload.round_id
                )
                if not entries:
                    raise ValueError("Ronda sin fills confirmados")
                if stamp.effective_at < entries[0].executed_at:
                    raise ValueError("Evidencia de posición anterior a su primer Fill confirmado")
                if isinstance(payload, (IsolatedMarginEvidence, MarginAdjustmentEvidence)):
                    if any(
                        point.at >= stamp.effective_at
                        and any(
                            p.round_id == payload.round_id
                            and p.isolated_wallet_balance.value is not None
                            for p in point.positions
                        )
                        for point in state.evaluations
                    ):
                        raise ValueError("Evidencia tardía contradice una asignación publicada")
                    if any(
                        type(e) is type(payload)
                        and getattr(e, "round_id", None) == payload.round_id
                        and e.stamp.effective_at == stamp.effective_at
                        and (
                            not isinstance(payload, MarginAdjustmentEvidence)
                            or isinstance(e, MarginAdjustmentEvidence)
                            and e.kind == payload.kind
                        )
                        for e in state.events
                    ):
                        raise ValueError("Hechos de margen económicos solapados")
                if isinstance(payload, IsolatedMarginEvidence):
                    if self.policy.initial != "EVIDENCE_ONLY":
                        raise ValueError("No mezclar balance absoluto y asignación por fill")
                    causal_ids = tuple(
                        e.fill_id for e in entries if e.executed_at <= stamp.effective_at
                    )
                    if payload.fill_ids != causal_ids:
                        raise ValueError("Balance sin FillIds causales exactos")
                    current_financial = next(
                        r for r in state.context.financial.rounds if r.round_id == payload.round_id
                    )
                    if payload.fill_ids == tuple(e.fill_id for e in entries):
                        for included, recognized in (
                            (payload.included_fees_usdc, current_financial.fees.value),
                            (payload.included_funding_usdc, current_financial.funding.value),
                        ):
                            if (
                                included is not None
                                and recognized is not None
                                and included != recognized
                            ):
                                raise ValueError("Balance contradice costos reconciliados")
                    if any(
                        isinstance(e, MarginAdjustmentEvidence)
                        and e.round_id == payload.round_id
                        and e.stamp.effective_at == stamp.effective_at
                        for e in state.events
                    ):
                        raise ValueError("Balance/ajuste simultáneos sin conciliación")
                elif isinstance(payload, MarginAdjustmentEvidence):
                    if any(
                        isinstance(e, IsolatedMarginEvidence)
                        and e.round_id == payload.round_id
                        and e.stamp.effective_at == stamp.effective_at
                        for e in state.events
                    ):
                        raise ValueError("Ajuste/balance simultáneos sin conciliación")
                    if payload.kind == "SELL_RELEASE":
                        if self.policy.release != "EXPLICIT_ONLY":
                            raise ValueError(
                                "Liberación explícita duplicaría política proporcional"
                            )
                        fill = next((e for e in entries if e.fill_id == payload.fill_id), None)
                        if (
                            fill is None
                            or fill.side is not Side.SELL
                            or fill.executed_at > stamp.effective_at
                        ):
                            raise ValueError("Liberación sin Fill SELL causal")
                        if any(
                            isinstance(e, MarginAdjustmentEvidence) and e.fill_id == payload.fill_id
                            for e in state.events
                        ):
                            raise ValueError("Liberación duplicada para el mismo FillId")
                elif any(
                    isinstance(e, RiskValuationEvidence)
                    and e.round_id == payload.round_id
                    and e.stamp.received_at == at
                    for e in state.events
                ):
                    raise ValueError("Hipótesis simultáneas sin selección explícita")
            return replace(state, events=state.events + (payload,))
        if payload != "VALUE" or state.context is None or state.context.at != at:
            raise ValueError("Evaluación sin contexto causal o transición desconocida")
        if state.evaluations and state.evaluations[-1].at == at:
            raise ValueError("Una evaluación después de todos los eventos del instante")
        return replace(state, evaluations=state.evaluations + (evaluate(state, state.context, at),))

    def validate(
        self,
        state: RiskState,
        at: datetime,
        performance: FinancialPerformance,
        financial: PerformanceState,
    ) -> int:
        self._time(state, at)
        rebuilt = self.initial(state.period_start, state.period_end)
        prefix = performance.initial(state.period_start, state.period_end)
        pending = tuple(
            sorted(self.source.read(0, at), key=lambda e: (e.stamp.received_at, e.stamp.sequence))
        )
        processed = 0

        def tick(current: RiskState, point: datetime) -> RiskState:
            nonlocal pending, processed
            current = self.observe(
                current, RiskObservation(point, self.context_from(performance, prefix, point))
            )
            while pending and pending[0].stamp.received_at == point and not current.conflicts:
                current = self.observe(current, RiskObservation(point, pending[0]))
                pending = pending[1:]
                processed += 1
            if not current.conflicts:
                current = self.observe(current, RiskObservation(point, "VALUE"))
            return current

        # La fuente y el journal financiero son las autoridades. No se confía en
        # una serie de riesgo auto-consistente pero reescrita junto a su checksum.
        for observation in financial.journal:
            if observation.at > at:
                raise ValueError("Journal financiero futuro respecto al riesgo")
            prefix = performance.observe(prefix, observation)
            if observation.payload == "VALUE":
                rebuilt = tick(rebuilt, observation.at)
                if rebuilt.conflicts:
                    break
        if not rebuilt.conflicts and financial.conflicts:
            for conflict in financial.conflicts:
                prefix = performance.observe(prefix, conflict.observation)
            rebuilt = tick(rebuilt, financial.conflicts[0].at)
        if rebuilt != state:
            raise ValueError("Snapshot de riesgo adulterado; reconstrucción distinta")
        return processed

    def report(
        self, state: RiskState, at: datetime, context: RiskContext | None = None
    ) -> RiskReport:
        self._time(state, at)
        with localcontext(ledger_context()):
            ctx = state.context if context is None else context
            if state.conflicts:
                ctx = state.context  # Conservar exposición del prefijo conciliado.
            if ctx is None:
                missing = Metric(None, "NOT_AVAILABLE", ("FINANCIAL_CONTEXT_MISSING",))
                current = RiskEvaluation(at, (), missing, missing)
            else:
                try:
                    current = evaluate(state, ctx, at)
                except (ValueError, DecimalException):
                    if not state.conflicts:
                        raise
                    # Un dato no representable ya registrado no debe impedir
                    # inspeccionar el bloqueo y su contexto/ledger completo.
                    current = (
                        replace(state.evaluations[-1], at=at)
                        if state.evaluations
                        else RiskEvaluation(at, (), ctx.financial.wallet, ctx.financial.equity)
                    )
            if state.conflicts:
                blocked = Metric(None, "BLOCKED", (state.conflicts[0].reason,))
                current = replace(
                    current,
                    account_wallet=current.account_wallet
                    if context is None
                    else context.financial.wallet,
                    account_equity=current.account_equity
                    if context is None
                    else context.financial.equity,
                    positions=tuple(
                        replace(
                            p,
                            isolated_wallet_balance=blocked,
                            isolated_margin_balance=blocked,
                            maintenance=blocked,
                            buffer=blocked,
                            margin_ratio=blocked,
                            threshold_price=blocked,
                            distance_to_threshold=blocked,
                            condition="UNKNOWN",
                            availability="BLOCKED",
                            hypothesis=None,
                        )
                        for p in current.positions
                    ),
                )
            breaches = sum(
                p.condition in {"BELOW_MAINTENANCE", "AT_MAINTENANCE_THRESHOLD"}
                for v in state.evaluations
                for p in v.positions
            )
            info = (
                "BLOCKED"
                if state.conflicts
                else "INSUFFICIENT_INFORMATION"
                if any(p.condition == "UNKNOWN" for p in current.positions)
                else "EVALUATED_BREACH_AT_SAMPLES"
                if breaches
                else "SAMPLED_VALUES_ONLY"
            )
            return RiskReport(
                self.identity,
                self.policy,
                self.source.identity,
                current,
                state.evaluations,
                state.journal,
                tuple(e.stamp.event_id for e in state.events),
                state.conflicts,
                breaches,
                info,
            )
