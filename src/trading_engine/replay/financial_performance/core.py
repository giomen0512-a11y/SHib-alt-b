"""Contabilidad/valoración offline sobre FinancialLedger intacto; nunca matching."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from hashlib import sha256

from trading_engine.models._validation import require_type
from trading_engine.models.enums import Side
from trading_engine.models.ids import RoundId
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_ledger.models import LedgerEntry, LedgerState
from trading_engine.replay.financial_ledger.policy import ExecutionOrigin, ledger_context
from trading_engine.replay.financial_performance.evidence import (
    CoverageEvidence,
    FeeEvidence,
    FinancialEvent,
    FinancialSource,
    FixtureFinancialSource,
    FixtureMarks,
    FundingEvidence,
    MarkEvidence,
    MarkPriceSource,
    Origin,
    TransferEvidence,
)
from trading_engine.replay.financial_performance.models import (
    FinancialObservation,
    Metric,
    PerformanceConflict,
    PerformanceReport,
    PerformanceState,
    RoundPerformance,
    Valuation,
)
from trading_engine.replay.financial_performance.policy import PerformancePolicy
from trading_engine.utils.time import require_utc

ZERO = Decimal("0")


def metric(value: Decimal | None, origins: tuple[Origin, ...] = (), *reasons: str) -> Metric:
    origins = tuple(sorted(set(origins)))
    status = (
        "NOT_AVAILABLE"
        if value is None
        else ("MODELED" if Origin.MODELED in origins else "AVAILABLE")
    )
    return Metric(value, status, tuple(reasons), origins)


def dependent(value: Decimal, *inputs: Metric) -> Metric:
    origins = tuple(o for m in inputs for o in m.provenances)
    missing = tuple(r for m in inputs if m.value is None for r in m.reasons)
    return metric(None if any(m.value is None for m in inputs) else value, origins, *missing)


@dataclass(frozen=True)
class FinancialPerformance:
    policy: PerformancePolicy
    source: FinancialSource = FixtureFinancialSource()
    marks: MarkPriceSource = FixtureMarks()

    def __post_init__(self) -> None:
        require_type(self.policy, PerformancePolicy, "policy")

    @property
    def identity(self) -> str:
        return (
            "offline-performance-session-v1:"
            + sha256(encoded((self.policy, self.source.identity, self.marks.identity))).hexdigest()
        )

    def initial(self, start: datetime, end: datetime) -> PerformanceState:
        require_utc(start)
        require_utc(end)
        if start >= end:
            raise ValueError("Intervalo financiero inválido")
        if self.policy.capital is not None and self.policy.capital.effective_at != start:
            raise ValueError("Capital inicial debe corresponder exactamente a start")
        return PerformanceState(self.identity, self.policy, start, end)

    def _time(self, state: PerformanceState, at: datetime) -> None:
        require_utc(at)
        if state.identity != self.identity or state.policy != self.policy:
            raise ValueError("Estado financiero de otra política/fuente/dataset")
        if not state.period_start <= at <= state.period_end:
            raise ValueError("Evento financiero fuera de período evaluado")
        if state.last_at is not None and at < state.last_at:
            raise ValueError("No modificar valoraciones pasadas")

    def observe(
        self, state: PerformanceState, observation: FinancialObservation
    ) -> PerformanceState:
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
                    PerformanceConflict(observation.at, f"{type(exc).__name__}:{exc}", observation),
                ),
                last_at=observation.at,
            )
        if updated == state:
            return state
        return replace(updated, journal=state.journal + (observation,), last_at=observation.at)

    def _observe(
        self, state: PerformanceState, observation: FinancialObservation
    ) -> PerformanceState:
        at, payload = observation.at, observation.payload
        if isinstance(payload, LedgerState):
            if payload.conflicts:
                raise ValueError("Ledger bruto bloqueado")
            FinancialLedger(payload.policy).validate(payload, at)
            previous = state.ledger
            if previous is not None:
                if (
                    payload.identity != previous.identity
                    or payload.entries[: len(previous.entries)] != previous.entries
                ):
                    raise ValueError("Ledger cambiado/reescrito")
                new_entries = payload.entries[len(previous.entries) :]
            else:
                new_entries = payload.entries
            for entry in new_entries:
                for event in state.events:
                    f = event.payload
                    if (
                        isinstance(f, FundingEvidence)
                        and f.round_id == entry.round_id
                        and entry.executed_at <= f.effective_at
                    ):
                        raise ValueError("Fill tardío cambia exposición funding ya reconocida")
            if previous == payload:
                return state
            return replace(state, ledger=payload)
        if isinstance(payload, FinancialEvent):
            if payload.received_at != at:
                raise ValueError("Evento fuera de su disponibilidad causal")
            p = payload.payload
            if p.effective_at < state.period_start:
                raise ValueError("Evidencia anterior al período financiero")
            prior = next((e for e in state.events if e.payload.event_id == p.event_id), None)
            if prior is not None:
                if prior.payload != p:
                    raise ValueError("Identidad financiera conflictiva; no sobrescribir")
                return state
            ledger = state.ledger
            if ledger is None:
                raise ValueError("Requiere vista del ledger bruto")
            if isinstance(p, FeeEvidence):
                fee_entry = next((e for e in ledger.entries if e.fill_id == p.fill_id), None)
                if (
                    fee_entry is None
                    or fee_entry.order_id != p.order_id
                    or fee_entry.executed_at != p.effective_at
                ):
                    raise ValueError("Fee no pertenece a Fill/Order/instante recibido")
                if any(
                    isinstance(e.payload, FeeEvidence) and e.payload.fill_id == p.fill_id
                    for e in state.events
                ):
                    raise ValueError("Dos comisiones para el mismo fill")
                if self.policy.fees is not None and any(
                    i == p.fill_id for i, _ in self.policy.fees.liquidity_by_fill
                ):
                    raise ValueError("Fee explícita y tasa modelada duplican el costo")
            elif isinstance(p, FundingEvidence):
                if p.symbol != Symbol("1000SHIBUSDC"):
                    raise ValueError("Funding de otro símbolo")
                if any(
                    isinstance(e.payload, FundingEvidence)
                    and (e.payload.round_id, e.payload.effective_at) == (p.round_id, p.effective_at)
                    for e in state.events
                ):
                    raise ValueError("Funding duplicado bajo otra identidad")
                exposure = self._exposure(ledger, p.round_id, p.effective_at)
                if exposure != p.exposure_quantity or exposure <= 0:
                    raise ValueError("Funding sin exposición histórica LONG demostrada")
            elif isinstance(p, MarkEvidence):
                if p.symbol != Symbol("1000SHIBUSDC"):
                    raise ValueError("Mark de otro símbolo")
                previous_marks = [
                    e.payload for e in state.events if isinstance(e.payload, MarkEvidence)
                ]
                if previous_marks and p.effective_at <= previous_marks[-1].effective_at:
                    raise ValueError("Mark tardío/conflictivo: no reescribir serie")
            elif isinstance(p, CoverageEvidence):
                if p.start < state.period_start or p.end > state.period_end:
                    raise ValueError("Cobertura fuera del período configurado")
            updated = replace(state, events=state.events + (payload,))
            self._check_inventory(updated)
            return updated
        if payload != "VALUE":
            raise ValueError("Transición financiera desconocida")
        if state.series and state.series[-1].at == at:
            raise ValueError("Solo una valoración después de todos los eventos del instante")
        value = self.value(state, at)
        peak, peak_at = state.peak_equity, state.peak_at
        maximum, max_pct = state.max_drawdown, state.max_drawdown_pct
        max_peak_at, valley_at = state.max_peak_at, state.max_valley_at
        if value.equity.value is not None:
            equity = value.equity.value
            if peak is None or equity > peak:
                peak, peak_at = equity, at
            assert peak is not None
            drawdown = peak - equity
            pct = drawdown / peak * Decimal("100") if peak > 0 else None
            if maximum is None or drawdown > maximum:
                maximum, max_peak_at, valley_at = drawdown, peak_at, at
            if pct is not None and (max_pct is None or pct > max_pct):
                max_pct = pct
        return replace(
            state,
            series=state.series + (value,),
            peak_equity=peak,
            peak_at=peak_at,
            max_drawdown=maximum,
            max_drawdown_pct=max_pct,
            max_peak_at=max_peak_at,
            max_valley_at=valley_at,
            invalid_samples=state.invalid_samples + int(value.equity.value is None),
        )

    @staticmethod
    def _exposure(ledger: LedgerState, round_id: RoundId, at: datetime) -> Decimal:
        relevant = [e for e in ledger.entries if e.round_id == round_id]
        if any(e.executed_at == at for e in relevant):
            raise ValueError("Funding/Fill simultáneos requieren secuencia económica adicional")

        def total(side: Side) -> Decimal:
            return sum(
                (
                    e.quantity
                    for e in sorted(relevant, key=lambda e: e.fill_id.int)
                    if e.executed_at < at and e.side is side
                ),
                ZERO,
            )

        return total(Side.BUY) - total(Side.SELL)

    @staticmethod
    def _check_inventory(state: PerformanceState) -> None:
        for event in state.events:
            coverage = event.payload
            if not isinstance(coverage, CoverageEvidence):
                continue
            cls = FundingEvidence if coverage.metric == "FUNDING" else TransferEvidence
            for other in state.events:
                p = other.payload
                if isinstance(p, cls) and coverage.start <= p.effective_at <= coverage.end:
                    if (
                        p.event_id not in coverage.event_ids
                        or p.reference.dataset != coverage.reference.dataset
                    ):
                        raise ValueError("Evento contradice inventario/cobertura declarados")
                if p.event_id in coverage.event_ids:
                    if (
                        not isinstance(p, cls)
                        or not coverage.start <= p.effective_at <= coverage.end
                    ):
                        raise ValueError("Inventario referencia otro tipo/período de evidencia")

    @staticmethod
    def _coverage(state: PerformanceState, name: str, start: datetime, end: datetime) -> Metric:
        if start == end:
            return metric(ZERO)
        covers = [
            e.payload
            for e in state.events
            if isinstance(e.payload, CoverageEvidence)
            and e.payload.metric == name
            and e.payload.reference.provenance is not Origin.UNKNOWN
        ]
        cursor = start
        origins: list[Origin] = []
        ids = {
            e.payload.event_id
            for e in state.events
            if e.payload.reference.provenance is not Origin.UNKNOWN
        }
        for cover in sorted(covers, key=lambda c: (c.start, c.end)):
            if cover.start <= cursor < cover.end and set(cover.event_ids) <= ids:
                cursor = cover.end
                origins.append(cover.reference.provenance)
                if cursor >= end:
                    return metric(ZERO, tuple(origins))
        result = metric(None, tuple(origins), name + "_COVERAGE_MISSING")
        return replace(result, status="INCOMPLETE" if covers else "NOT_AVAILABLE")

    def _fees(self, state: PerformanceState, entries: tuple[LedgerEntry, ...]) -> Metric:
        values: list[Decimal] = []
        origins: list[Origin] = []
        missing: list[str] = []
        for entry in sorted(entries, key=lambda e: e.fill_id.int):
            evidence = next(
                (
                    e.payload
                    for e in state.events
                    if isinstance(e.payload, FeeEvidence) and e.payload.fill_id == entry.fill_id
                ),
                None,
            )
            if (
                evidence is not None
                and evidence.currency == "USDC"
                and evidence.reference.provenance is not Origin.UNKNOWN
            ):
                values.append(evidence.amount)
                origins.append(evidence.reference.provenance)
                continue
            policy = self.policy.fees
            role = (
                None
                if policy is None
                else next(
                    (role for i, role in policy.liquidity_by_fill if i == entry.fill_id), None
                )
            )
            if role is not None:
                assert policy is not None
                rate = policy.maker_rate if role == "MAKER" else policy.taker_rate
                fee = entry.notional_usdc * rate
                if rate > 0 and fee <= 0:
                    raise ValueError("Fee positiva no representable")
                values.append(fee)
                origins.append(Origin.MODELED)
            else:
                missing.append("FEE_MISSING_OR_CURRENCY_UNCONVERTED:" + str(entry.fill_id))
        result = metric(None if missing else sum(values, ZERO), tuple(origins), *missing)
        return replace(result, status="INCOMPLETE") if missing and values else result

    def _funding(
        self,
        state: PerformanceState,
        start: datetime,
        end: datetime,
        round_id: RoundId | None = None,
    ) -> Metric:
        coverage = self._coverage(state, "FUNDING", start, end)
        values: list[Decimal] = []
        origins: list[Origin] = list(coverage.provenances)
        unknown = False
        for event in state.events:
            p = event.payload
            if not isinstance(p, FundingEvidence) or not start <= p.effective_at <= end:
                continue
            if round_id is not None and p.round_id != round_id:
                continue
            if p.reference.provenance is Origin.UNKNOWN:
                unknown = True
                continue
            value = p.amount_usdc
            if value is None:
                assert p.reference_price is not None and p.rate is not None
                notional = p.exposure_quantity * p.reference_price
                if notional <= 0:
                    raise ValueError("Nocional funding positivo no representable")
                value = -notional * p.rate
                if p.rate != 0 and value == 0:
                    raise ValueError("Funding no nulo no representable")
            values.append(value)
            origins.append(p.reference.provenance)
        return metric(
            None if coverage.value is None or unknown else sum(values, ZERO),
            tuple(origins),
            *(coverage.reasons + (("FUNDING_UNKNOWN",) if unknown else ())),
        )

    def value(self, state: PerformanceState, at: datetime) -> Valuation:
        self._time(state, at)
        with localcontext(ledger_context()):
            ledger = state.ledger
            if ledger is None:
                raise ValueError("No existe ledger bruto para valorar")
            ledger_report = FinancialLedger(ledger.policy).report(ledger)
            origins = tuple(
                Origin.MODELED if e.provenance is ExecutionOrigin.MODELED else Origin.FIXTURE
                for e in ledger.entries
            )
            gross = metric(ledger_report.gross_realized_pnl_usdc, origins)
            fees = self._fees(state, ledger.entries)
            funding = self._funding(state, state.period_start, at)
            transfers = self._coverage(state, "TRANSFERS", state.period_start, at)
            deposits, withdrawals = ZERO, ZERO
            transfer_origins = list(transfers.provenances)
            unknown = False
            for event in state.events:
                p = event.payload
                if isinstance(p, TransferEvidence):
                    if p.reference.provenance is Origin.UNKNOWN:
                        unknown = True
                    elif p.kind == "DEPOSIT":
                        deposits += p.amount_usdc
                    else:
                        withdrawals += p.amount_usdc
                    transfer_origins.append(p.reference.provenance)
            deposit = metric(
                None if transfers.value is None or unknown else deposits,
                tuple(transfer_origins),
                *transfers.reasons,
            )
            withdrawal = metric(
                None if transfers.value is None or unknown else withdrawals,
                tuple(transfer_origins),
                *transfers.reasons,
            )
            cash = dependent(
                (gross.value or ZERO) - (fees.value or ZERO) + (funding.value or ZERO),
                gross,
                fees,
                funding,
            )
            marks = [e.payload for e in state.events if isinstance(e.payload, MarkEvidence)]
            mark = marks[-1] if marks else None
            unrealized = metric(None, (), "MARK_MISSING_STALE_OR_DISCONTINUOUS")
            if ledger_report.open_quantity == 0:
                unrealized = metric(ZERO, origins)
            elif (
                mark is not None
                and mark.continuous
                and mark.reference.provenance is not Origin.UNKNOWN
                and mark.effective_at
                <= at
                <= mark.effective_at + timedelta(milliseconds=mark.freshness_limit_ms)
            ):
                mark_notional = ledger_report.open_quantity * mark.price
                if mark_notional <= 0:
                    raise ValueError("Nocional Mark positivo no representable")
                unrealized = metric(
                    mark_notional - ledger_report.open_cost_basis_usdc,
                    origins + (mark.reference.provenance,),
                )
            capital = self.policy.capital
            initial = (
                metric(None, (), "CAPITAL_MISSING")
                if capital is None
                else metric(capital.balance_usdc, (capital.reference.provenance,))
            )
            wallet = dependent(
                (initial.value or ZERO)
                + (deposit.value or ZERO)
                - (withdrawal.value or ZERO)
                + (cash.value or ZERO),
                initial,
                deposit,
                withdrawal,
                cash,
            )
            equity = dependent(
                (wallet.value or ZERO) + (unrealized.value or ZERO), wallet, unrealized
            )
            rounds = []
            for r in ledger.round_ledger:
                entries = tuple(e for e in ledger.entries if e.round_id == r.round_id)
                r_fees = self._fees(state, entries)
                finish = r.closed_at or at
                r_funding = self._funding(state, entries[0].executed_at, finish, r.round_id)
                r_gross = metric(
                    r.gross_realized_pnl_usdc,
                    tuple(
                        Origin.MODELED
                        if e.provenance is ExecutionOrigin.MODELED
                        else Origin.FIXTURE
                        for e in entries
                    ),
                )
                r_cash = dependent(
                    (r_gross.value or ZERO) - (r_fees.value or ZERO) + (r_funding.value or ZERO),
                    r_gross,
                    r_fees,
                    r_funding,
                )
                final = (
                    r_cash
                    if r.closed_at is not None and r.open_quantity == 0 and r.sell_quantity > 0
                    else metric(None, (), "ROUND_OPEN_NOT_FINAL")
                )
                rounds.append(
                    RoundPerformance(
                        r.round_id, r.open_quantity, r_gross, r_fees, r_funding, r_cash, final
                    )
                )
            return Valuation(
                at,
                gross,
                fees,
                funding,
                deposit,
                withdrawal,
                cash,
                unrealized,
                wallet,
                equity,
                None if mark is None else mark.event_id,
                wallet.value is not None and wallet.value < 0,
                tuple(rounds),
            )

    def validate(self, state: PerformanceState, at: datetime, ledger: LedgerState) -> None:
        self._time(state, at)
        rebuilt = self.initial(state.period_start, state.period_end)
        for observation in state.journal:
            if observation.at > at:
                raise ValueError("Journal financiero futuro")
            rebuilt = self.observe(rebuilt, observation)
            if rebuilt.conflicts:
                raise ValueError("Journal financiero incompatible")
        for conflict in state.conflicts:
            if conflict.at > at:
                raise ValueError("Conflicto financiero futuro")
            rebuilt = self.observe(rebuilt, conflict.observation)
        ledger_matches = state.ledger == ledger or any(
            conflict.observation.payload == ledger for conflict in state.conflicts
        )
        if rebuilt != state or not ledger_matches:
            raise ValueError("Snapshot de performance adulterado/incompatible con ledger")

    def report(self, state: PerformanceState, at: datetime) -> PerformanceReport:
        with localcontext(ledger_context()):
            try:
                current = self.value(state, at)
            except (ValueError, DecimalException):
                if not state.conflicts:
                    raise
                blocked = Metric(None, "BLOCKED", (state.conflicts[0].reason,))
                current = Valuation(
                    at,
                    blocked,
                    blocked,
                    blocked,
                    blocked,
                    blocked,
                    blocked,
                    blocked,
                    blocked,
                    blocked,
                    None,
                    False,
                    (),
                )
            origins = tuple(o for point in state.series for o in point.equity.provenances)
            coverage = (
                "PARTIAL"
                if state.invalid_samples or current.equity.value is None
                else "COMPLETE_AT_SAMPLED_INSTANTS"
            )
            peak = state.peak_equity
            dd = (
                None
                if peak is None or current.equity.value is None
                else max(ZERO, peak - current.equity.value)
            )
            pct = None if dd is None or peak is None or peak <= 0 else dd / peak * Decimal("100")

            def drawdown(value: Decimal | None) -> Metric:
                result = metric(
                    value, origins, "SAMPLED_SERIES_ONLY", "EXTERNAL_FLOWS_NOT_RETURN_ADJUSTED"
                )
                if state.conflicts:
                    return replace(result, value=None, status="BLOCKED")
                if coverage == "PARTIAL" and value is not None:
                    return replace(result, status="INCOMPLETE")
                return result

            if state.conflicts:
                blocked = Metric(None, "BLOCKED", (state.conflicts[0].reason,))
                current = replace(
                    current,
                    fees=blocked,
                    funding=blocked,
                    net_cash=blocked,
                    unrealized=blocked,
                    wallet=blocked,
                    equity=blocked,
                    rounds=tuple(
                        replace(
                            r,
                            fees=blocked,
                            funding=blocked,
                            net_cash=blocked,
                            net_realized_closed=blocked,
                        )
                        for r in current.rounds
                    ),
                )
            return PerformanceReport(
                self.identity,
                state.period_start,
                state.period_end,
                self.policy,
                self.source.identity,
                self.marks.identity,
                current,
                state.series,
                peak,
                state.peak_at,
                drawdown(dd),
                drawdown(pct),
                drawdown(state.max_drawdown),
                drawdown(state.max_drawdown_pct),
                state.max_peak_at,
                state.max_valley_at,
                coverage,
                tuple(
                    e.payload.event_id
                    for e in state.events
                    if isinstance(e.payload, CoverageEvidence)
                ),
                tuple(e.payload.event_id for e in state.events),
                state.journal,
                state.conflicts,
            )
