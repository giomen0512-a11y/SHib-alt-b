"""Un único motor puro: señales, intención Base y hechos Order/Fill explícitos."""

from dataclasses import dataclass, replace
from datetime import datetime

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.account import Position
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import StrategyResult, StrategyState
from trading_engine.models.enums import RoundStatus
from trading_engine.models.ids import ActionId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order
from trading_engine.models.rounds import Round
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.base import (
    BaseState,
    base_intent,
    observe_fill,
    observe_order,
)
from trading_engine.strategy.alt_b.events.state import (
    AltBStrategyState,
    PendingEntry,
    entry_identity,
)
from trading_engine.strategy.alt_b.state_market import MarketIngestResult, ingest_closed_1m


@dataclass(frozen=True)
class AltBEngine:
    config: AltBConfig = AltBConfig()

    def __post_init__(self) -> None:
        require_type(self.config, AltBConfig, "config")

    def _snapshot(self, state: StrategyState | None) -> AltBStrategyState:
        if state is None:
            return AltBStrategyState()
        require_type(state, AltBStrategyState, "strategy_state")
        assert isinstance(state, AltBStrategyState)
        if state.market.candles_1m and state.market.candles_1m[-1].symbol != self.config.symbol:
            raise ValueError("Estado de otro símbolo")
        if state.pending_entry is not None:
            expected = entry_identity(self.config, state.pending_entry.signal.timestamp)
            if (state.pending_entry.signal, state.pending_entry.action) != expected:
                raise ValueError("Identidad pendiente incompatible con la configuración")
        if state.base is not None:
            base = state.base
            if base.action.symbol != self.config.symbol or base.intent != base_intent(
                base.action, base.reference_quote, self.config
            ):
                raise ValueError("Base incompatible con la configuración")
        return state

    @staticmethod
    def _cancel(
        state: AltBStrategyState, reason: str
    ) -> tuple[AltBStrategyState, tuple[ActionId, ...]]:
        ids = () if state.pending_entry is None else (state.pending_entry.action.action_id,)
        if state.block_reason == "execution_without_base_reservation":
            reason = state.block_reason
        return replace(state, pending_entry=None, block_reason=reason), ids

    def _prepare(
        self,
        state: AltBStrategyState,
        at: datetime,
        position: Position | None,
        round_state: Round | None,
        *,
        execution_fact: bool = False,
    ) -> tuple[AltBStrategyState, tuple[ActionId, ...]]:
        utc_timestamp(at, "event_time")
        if not execution_fact and state.last_event_at is not None and at < state.last_event_at:
            raise ValueError("Evento anterior al cursor causal; no usar contexto futuro")
        reason: str | None = None
        if not state.allow_entries:
            reason = "entries_paused"
        if position is not None:
            require_type(position, Position, "position")
            reason = "position_present"
        if round_state is not None:
            require_type(round_state, Round, "round_state")
            times = (round_state.created_at, round_state.opened_at, round_state.closed_at)
            if round_state.symbol != self.config.symbol or any(
                t is not None and t > at for t in times
            ):
                reason = "round_context_unverifiable"
            elif round_state.status in (
                RoundStatus.PENDING_ENTRY,
                RoundStatus.OPEN,
                RoundStatus.CLOSING,
            ):
                own_reservation = (
                    round_state.status is RoundStatus.PENDING_ENTRY
                    and state.pending_entry is not None
                    and round_state.round_id == state.pending_entry.action.round_id
                )
                if not own_reservation:
                    reason = "round_active"
        state = replace(state, last_event_at=max(at, state.last_event_at or at))
        if reason is not None:
            return self._cancel(state, reason)
        if state.block_reason == "execution_without_base_reservation":
            return state, ()
        return replace(state, block_reason=state.base.block_reason if state.base else None), ()

    @staticmethod
    def _result(state: AltBStrategyState, cancelled: tuple[ActionId, ...] = ()) -> StrategyResult:
        return StrategyResult(strategy_state=state, cancelled_action_ids=cancelled)

    def on_candle(
        self,
        candle: Candle,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        require_type(candle, Candle, "candle")
        state = self._snapshot(strategy_state)
        previous_reason = state.block_reason
        # Un retry idéntico no reutiliza una autorización posterior para emitir la señal antigua.
        duplicate = bool(state.market.candles_1m and candle == state.market.candles_1m[-1])
        at = (
            max(candle.close_time, state.last_event_at or candle.close_time)
            if duplicate
            else candle.close_time
        )
        state, cancelled = self._prepare(state, at, position, round_state)
        step = ingest_closed_1m(state.market, candle, self.config)
        state = replace(state, market=step.state)
        if duplicate and not cancelled and state.block_reason is None:
            state = replace(state, block_reason=previous_reason)
        if step.result is MarketIngestResult.GAP_RESET:
            state, extra = self._cancel(state, "gap_reset")
            cancelled += extra
        if duplicate or step.evaluation is None:
            return self._result(state, cancelled)
        evaluation = step.evaluation
        if state.block_reason is not None:
            return self._result(state, cancelled)
        if state.pending_entry is not None:
            return self._result(replace(state, block_reason="entry_pending"), cancelled)
        if not evaluation.data_valid or not evaluation.entry_allowed:
            return self._result(replace(state, block_reason=evaluation.reason), cancelled)
        signal, action = entry_identity(self.config, evaluation.checkpoint_at)
        state = replace(state, pending_entry=PendingEntry(signal, action))
        return StrategyResult(
            signals=(signal,),
            scheduled_actions=(action,),
            strategy_state=state,
            cancelled_action_ids=cancelled,
        )

    def on_quote(
        self,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        require_type(quote, Quote, "quote")
        if quote.symbol != self.config.symbol:
            raise ValueError("Quote de otro símbolo")
        state, cancelled = self._prepare(
            self._snapshot(strategy_state), quote.timestamp, position, round_state
        )
        return self._result(state, cancelled)

    def on_scheduled_action(
        self,
        action: ScheduledAction,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        require_type(action, ScheduledAction, "action")
        require_type(quote, Quote, "quote")
        if quote.timestamp < action.not_before:
            raise ValueError("Quote anterior a not_before")
        state, cancelled = self._prepare(
            self._snapshot(strategy_state), quote.timestamp, position, round_state
        )
        if state.base is not None:
            # Reserva consumida: ningún retry ni otra acción puede emitir segunda Base.
            return self._result(state, tuple(dict.fromkeys((*cancelled, action.action_id))))
        pending = state.pending_entry
        if (
            pending is None
            or action != pending.action
            or quote.symbol != action.symbol
            or action.symbol != self.config.symbol
        ):
            reason = state.block_reason or "pending_entry_unverifiable"
            state, extra = self._cancel(state, reason)
            return self._result(state, tuple(dict.fromkeys((*cancelled, *extra, action.action_id))))
        if pending.evaluated_at is not None:
            # Un snapshot bloqueado de la fase anterior requiere revisión explícita.
            return self._result(replace(state, block_reason="entry_requires_reconciliation"))
        intent = base_intent(action, quote, self.config)
        base = BaseState(action, quote, intent)
        state = replace(state, pending_entry=None, base=base, block_reason=base.block_reason)
        return StrategyResult(
            order_intents=(intent,),
            strategy_state=state,
            cancelled_action_ids=tuple(dict.fromkeys((*cancelled, action.action_id))),
        )

    def on_entry_control(
        self,
        allow_entries: bool,
        at: datetime,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        require_type(allow_entries, bool, "allow_entries")
        state = self._snapshot(strategy_state)
        cancelled: tuple[ActionId, ...] = ()
        if not allow_entries:
            state, cancelled = self._cancel(state, "entries_paused")
        state = replace(state, allow_entries=allow_entries)
        state, extra = self._prepare(state, at, position, round_state)
        return self._result(state, tuple(dict.fromkeys((*cancelled, *extra))))

    def on_action_invalidated(
        self,
        action_id: ActionId,
        at: datetime,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        StrategyResult(cancelled_action_ids=(action_id,))
        state, cancelled = self._prepare(self._snapshot(strategy_state), at, position, round_state)
        if state.pending_entry is not None and state.pending_entry.action.action_id == action_id:
            state, extra = self._cancel(state, "entry_action_invalidated")
            cancelled += extra
        elif state.pending_entry is None and state.block_reason is None:
            state = replace(state, block_reason="entry_action_invalidated")
        return self._result(state, tuple(dict.fromkeys((*cancelled, action_id))))

    def on_order(
        self,
        order: Order,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        require_type(order, Order, "order")
        state, cancelled = self._prepare(
            self._snapshot(strategy_state),
            order.updated_at,
            position,
            round_state,
            execution_fact=True,
        )
        if state.base is not None:
            base = observe_order(state.base, order)
            return self._result(
                replace(state, base=base, block_reason=base.block_reason), cancelled
            )
        state, extra = self._cancel(state, "execution_without_base_reservation")
        return self._result(
            replace(state, allow_entries=False), tuple(dict.fromkeys((*cancelled, *extra)))
        )

    def on_fill(
        self,
        fill: Fill,
        order: Order,
        position: Position | None,
        round_state: Round | None,
        *,
        strategy_state: StrategyState | None = None,
    ) -> StrategyResult:
        require_type(fill, Fill, "fill")
        require_type(order, Order, "order")
        if fill.order_id != order.order_id:
            raise ValueError("Fill de otra orden")
        state, cancelled = self._prepare(
            self._snapshot(strategy_state),
            max(fill.timestamp, order.updated_at),
            position,
            round_state,
            execution_fact=True,
        )
        if state.base is not None:
            base = observe_fill(observe_order(state.base, order), fill)
            return self._result(
                replace(state, base=base, block_reason=base.block_reason), cancelled
            )
        state, extra = self._cancel(state, "execution_without_base_reservation")
        return self._result(
            replace(state, allow_entries=False), tuple(dict.fromkeys((*cancelled, *extra)))
        )
