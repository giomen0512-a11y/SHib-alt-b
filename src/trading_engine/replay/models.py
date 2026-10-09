"""Eventos, checkpoint y auditoría offline; sin un modelo de ejecución."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order, OrderCancelRequest, OrderIntent
from trading_engine.models.signals import Signal
from trading_engine.orchestration.models import CoordinatorState
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.state import AltBStrategyState


@dataclass(frozen=True)
class EntryControlEvent:
    at: datetime
    allow_entries: bool

    def __post_init__(self) -> None:
        utc_timestamp(self.at, "at")
        require_type(self.allow_entries, bool, "allow_entries")


@dataclass(frozen=True)
class FillEvent:
    fill: Fill
    order: Order

    def __post_init__(self) -> None:
        require_type(self.fill, Fill, "fill")
        require_type(self.order, Order, "order")
        if self.fill.order_id != self.order.order_id:
            raise ValueError("Fill de otra Order")


type ReplayEvent = EntryControlEvent | Order | FillEvent | Candle | Quote
type EventKey = tuple[datetime, int]


def event_key(event: ReplayEvent) -> EventKey:
    """Prioridad explícita; nunca deduce una trayectoria dentro de una vela."""
    if isinstance(event, EntryControlEvent):
        return event.at, 0
    if isinstance(event, Order):
        return event.updated_at, 1
    if isinstance(event, FillEvent):
        return max(event.fill.timestamp, event.order.updated_at), 2
    if isinstance(event, Candle):
        return event.close_time, 3
    if isinstance(event, Quote):
        return event.timestamp, 4
    raise TypeError("Evento Replay no admitido")


@dataclass(frozen=True)
class Gap:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for at in (self.start, self.end):
            utc_timestamp(at, "gap")
            if at.second or at.microsecond:
                raise ValueError("Gap no alineado a minuto")
        if self.end <= self.start:
            raise ValueError("Gap vacío o invertido")

    @property
    def minutes(self) -> int:
        return (self.end - self.start) // timedelta(minutes=1)


def merge_gaps(gaps: tuple[Gap, ...]) -> tuple[Gap, ...]:
    """Unir intervalos verificados adyacentes, también entre segmentos."""
    require_type(gaps, tuple, "gaps")
    for gap in gaps:
        require_type(gap, Gap, "gap")
    result: list[Gap] = []
    for gap in sorted(gaps, key=lambda g: g.start):
        if result and gap.start <= result[-1].end:
            result[-1] = Gap(result[-1].start, max(result[-1].end, gap.end))
        else:
            result.append(gap)
    return tuple(result)


@dataclass(frozen=True)
class ActionEvaluation:
    action: ScheduledAction
    quote: Quote

    def __post_init__(self) -> None:
        require_type(self.action, ScheduledAction, "action")
        require_type(self.quote, Quote, "quote")
        if self.quote.symbol != self.action.symbol or self.quote.timestamp < self.action.not_before:
            raise ValueError("Quote no admisible para acción")


@dataclass(frozen=True)
class ReplayOutputs:
    signals: tuple[Signal, ...] = ()
    scheduled_actions: tuple[ScheduledAction, ...] = ()
    evaluated_actions: tuple[ActionEvaluation, ...] = ()
    order_intents: tuple[OrderIntent, ...] = ()
    cancel_requests: tuple[OrderCancelRequest, ...] = ()
    observed_orders: tuple[Order, ...] = ()
    observed_fills: tuple[Fill, ...] = ()

    def __post_init__(self) -> None:
        for name, kind in (
            ("signals", Signal),
            ("scheduled_actions", ScheduledAction),
            ("evaluated_actions", ActionEvaluation),
            ("order_intents", OrderIntent),
            ("cancel_requests", OrderCancelRequest),
            ("observed_orders", Order),
            ("observed_fills", Fill),
        ):
            values = getattr(self, name)
            require_type(values, tuple, name)
            for value in values:
                require_type(value, kind, name)
        for identifiers in (
            tuple(s.signal_id for s in self.signals),
            tuple(a.action_id for a in self.scheduled_actions),
            tuple(e.action.action_id for e in self.evaluated_actions),
            tuple(i.intent_id for i in self.order_intents),
            tuple(r.intent_id for r in self.cancel_requests),
            tuple(f.fill_id for f in self.observed_fills),
        ):
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Identidad duplicada en outputs Replay")


@dataclass(frozen=True)
class ReplayBlock:
    at: datetime
    reason: str

    def __post_init__(self) -> None:
        utc_timestamp(self.at, "at")
        require_type(self.reason, str, "reason")
        if not self.reason.strip():
            raise ValueError("Motivo vacío")


@dataclass(frozen=True)
class ReplayAudit:
    range_start: datetime | None = None
    range_end: datetime | None = None
    candles_processed: int = 0
    checkpoints_evaluated: int = 0
    gaps: tuple[Gap, ...] = ()
    blocks: tuple[ReplayBlock, ...] = ()
    rounds_opened: int = 0

    def __post_init__(self) -> None:
        if (self.range_start is None) != (self.range_end is None):
            raise ValueError("Rango incompleto")
        if self.range_start is not None and self.range_end is not None:
            utc_timestamp(self.range_start, "range_start")
            utc_timestamp(self.range_end, "range_end")
            if self.range_start > self.range_end:
                raise ValueError("Rango invertido")
        for count in (self.candles_processed, self.checkpoints_evaluated, self.rounds_opened):
            require_type(count, int, "count")
            if count < 0:
                raise ValueError("Contador negativo")
        object.__setattr__(self, "gaps", merge_gaps(self.gaps))
        require_type(self.blocks, tuple, "blocks")
        for block in self.blocks:
            require_type(block, ReplayBlock, "block")

    @property
    def minutes_missing(self) -> int:
        return sum(g.minutes for g in self.gaps)


@dataclass(frozen=True)
class ReplayState:
    config: AltBConfig = AltBConfig()
    coordinator: CoordinatorState = field(
        default_factory=lambda: CoordinatorState(strategy_state=AltBStrategyState())
    )
    through: datetime | None = None
    last_key: EventKey | None = None
    sqlite_end: datetime | None = None
    outputs: ReplayOutputs = ReplayOutputs()
    audit: ReplayAudit = ReplayAudit()

    def __post_init__(self) -> None:
        require_type(self.config, AltBConfig, "config")
        if self.config.symbol.value != "1000SHIBUSDC":
            raise ValueError("Replay 3C-1 requiere 1000SHIBUSDC Futures")
        require_type(self.coordinator, CoordinatorState, "coordinator")
        require_type(self.coordinator.strategy_state, AltBStrategyState, "strategy_state")
        require_type(self.outputs, ReplayOutputs, "outputs")
        require_type(self.audit, ReplayAudit, "audit")
        for at in (self.through, self.sqlite_end):
            if at is not None:
                utc_timestamp(at, "cursor")
        if self.last_key is not None:
            require_type(self.last_key, tuple, "last_key")
            at, priority = self.last_key
            utc_timestamp(at, "last_key")
            require_type(priority, int, "priority")
            if priority not in range(5) or self.through is None or at > self.through:
                raise ValueError("Cursor de eventos incoherente")
        logical_at = self.strategy_state.last_event_at
        if logical_at is not None and (self.through is None or logical_at > self.through):
            raise ValueError("Estado de estrategia posterior al cursor Replay")
        if (self.last_key is None) != (logical_at is None) or (
            self.last_key is not None and self.last_key[0] != logical_at
        ):
            raise ValueError("Cursor Replay no coincide con watermark de estrategia")
        observed_times = (
            self.audit.range_end,
            *(b.at for b in self.audit.blocks),
            *(s.timestamp for s in self.outputs.signals),
            *(a.created_at for a in self.outputs.scheduled_actions),
            *(e.quote.timestamp for e in self.outputs.evaluated_actions),
            *(i.created_at for i in self.outputs.order_intents),
            *(r.created_at for r in self.outputs.cancel_requests),
            *(o.updated_at for o in self.outputs.observed_orders),
            *(f.timestamp for f in self.outputs.observed_fills),
        )
        if any(
            at is not None and (self.through is None or at > self.through) for at in observed_times
        ):
            raise ValueError("Evidencia Replay posterior al cursor")
        if self.sqlite_end is not None and (self.through is None or self.sqlite_end > self.through):
            raise ValueError("Rango SQLite posterior al cursor Replay")

    @property
    def strategy_state(self) -> AltBStrategyState:
        logical = self.coordinator.strategy_state
        assert isinstance(logical, AltBStrategyState)
        return logical

    @property
    def due_actions(self) -> tuple[ScheduledAction, ...]:
        return tuple(
            a
            for a in self.coordinator.pending_actions
            if self.through is not None and a.not_before <= self.through
        )

    def report(self) -> dict[str, object]:
        """Auditoría estructurada; el snapshot completo sigue en coordinator."""
        logical = self.strategy_state
        outputs = self.outputs
        active = logical.round_state
        pending_cancels = active.pending_cancellations() if active is not None else ()
        if logical.base_cancel_request is not None and logical.base is not None:
            if logical.base.order_pending or logical.base.reconciliation_required:
                pending_cancels += (logical.base_cancel_request,)
        return {
            "symbol": self.config.symbol.value,
            "market": "BINANCE_USDM_FUTURES",
            "range_start": self.audit.range_start,
            "range_end": self.audit.range_end,
            "gaps": self.audit.gaps,
            "minutes_missing": self.audit.minutes_missing,
            "checkpoints_evaluated": self.audit.checkpoints_evaluated,
            "OBSERVADO": {
                "candles": self.audit.candles_processed,
                "orders": len(outputs.observed_orders),
                "fills": len(outputs.observed_fills),
            },
            "SIMULADO": {
                "signals": len(outputs.signals),
                "scheduled_actions": len(outputs.scheduled_actions),
                "evaluated_actions": len(outputs.evaluated_actions),
                "order_intents": len(outputs.order_intents),
                "base_intents": sum(i.order_type.value == "MARKET" for i in outputs.order_intents),
                "dca_intents": sum(
                    i.order_type.value == "LIMIT" and not i.reduce_only
                    for i in outputs.order_intents
                ),
                "tp_intents": sum(i.reduce_only for i in outputs.order_intents),
                "cancel_requests": len(outputs.cancel_requests),
                "rounds_opened": self.audit.rounds_opened,
                "rounds_closed": sum(
                    r.round_state.can_close(r.base) for r in logical.round_history
                ),
            },
            "PENDIENTE": {
                "scheduled_actions": self.coordinator.pending_actions,
                "due_without_quote": self.due_actions,
                "cancel_requests": pending_cancels,
                "active_round": active,
                "base": logical.base,
            },
            "NO VERIFICABLE": (
                "historical_quotes",
                "execution_model",
                "profitability",
                "fees",
                "funding",
            ),
            "blocks": self.audit.blocks,
            "final_state": self.coordinator,
        }
