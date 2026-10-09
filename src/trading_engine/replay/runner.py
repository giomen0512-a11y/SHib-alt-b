"""Runner causal finito; inputs explícitos y un único AltBEngine."""

from collections.abc import Generator, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast

from trading_engine.models._validation import require_type, utc_timestamp
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Order
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.persistence.futures_sqlite import FuturesSQLite
from trading_engine.replay.models import (
    ActionEvaluation,
    EntryControlEvent,
    FillEvent,
    Gap,
    ReplayBlock,
    ReplayEvent,
    ReplayState,
    event_key,
)
from trading_engine.strategy.alt_b.config import AltBConfig
from trading_engine.strategy.alt_b.events.engine import AltBEngine
from trading_engine.strategy.alt_b.events.state import AltBStrategyState


def _merge(candles: Iterator[Candle], events: Iterator[ReplayEvent]) -> Generator[ReplayEvent]:
    """Dos streams ordenados; como máximo un input anticipado por stream."""
    candle = next(candles, None)
    event = next(events, None)
    while candle is not None or event is not None:
        if candle is not None and (event is None or event_key(candle) <= event_key(event)):
            yield candle
            candle = next(candles, None)
        else:
            assert event is not None
            yield event
            event = next(events, None)


@dataclass(frozen=True)
class ReplayRunner:
    config: AltBConfig = AltBConfig()

    def __post_init__(self) -> None:
        require_type(self.config, AltBConfig, "config")
        ReplayState(config=self.config)

    def run(self, events: Iterable[ReplayEvent], state: ReplayState | None = None) -> ReplayState:
        current = state if state is not None else ReplayState(config=self.config)
        require_type(current, ReplayState, "state")
        if current.config != self.config:
            raise ValueError("Reanudación con otra configuración")
        coordinator = StrategyCoordinator(AltBEngine(self.config))
        for event in events:
            key = event_key(event)
            at = key[0]
            if (current.through is not None and at < current.through) or (
                current.last_key is not None and key < current.last_key
            ):
                raise ValueError("Eventos fuera de orden causal (tiempo, prioridad)")
            previous = current.strategy_state
            before = current.coordinator
            duplicate = isinstance(event, Candle) and bool(
                previous.market.candles_1m and event == previous.market.candles_1m[-1]
            )
            if isinstance(event, EntryControlEvent):
                result = coordinator.on_entry_control(before, event.allow_entries, event.at)
            elif isinstance(event, Candle):
                result = coordinator.on_candle(before, event)
            elif isinstance(event, Quote):
                result = coordinator.on_quote(before, event)
            elif isinstance(event, Order):
                result = coordinator.on_order(before, event)
            else:
                result = coordinator.on_fill(before, event.fill, event.order)
            after = result.state
            evaluated: list[ActionEvaluation] = []
            signals = result.signals
            intents = result.order_intents
            cancels = result.cancel_requests
            scheduled = tuple(a for a in after.pending_actions if a not in before.pending_actions)
            if isinstance(event, Quote):
                due = coordinator.advance_time(after, at)
                after = due.state
                for action in due.due_actions:
                    decision = coordinator.on_scheduled_action(after, action, event)
                    after = decision.state
                    evaluated.append(ActionEvaluation(action, event))
                    signals += decision.signals
                    intents += decision.order_intents
                    cancels += decision.cancel_requests
            outputs = current.outputs
            observed_orders = outputs.observed_orders
            observed_fills = outputs.observed_fills
            order = event.order if isinstance(event, FillEvent) else event
            if isinstance(order, Order) and order not in observed_orders:
                observed_orders += (order,)
            if isinstance(event, FillEvent) and event.fill not in observed_fills:
                observed_fills += (event.fill,)
            outputs = replace(
                outputs,
                signals=outputs.signals + signals,
                scheduled_actions=outputs.scheduled_actions + scheduled,
                evaluated_actions=outputs.evaluated_actions + tuple(evaluated),
                order_intents=outputs.order_intents + intents,
                cancel_requests=outputs.cancel_requests + cancels,
                observed_orders=observed_orders,
                observed_fills=observed_fills,
            )
            audit = current.audit
            gaps = audit.gaps
            if isinstance(event, Candle) and not duplicate and previous.market.candles_1m:
                last = previous.market.candles_1m[-1]
                if event.open_time > last.close_time:
                    gaps += (Gap(last.close_time, event.open_time),)
            start = event.open_time if isinstance(event, Candle) else at
            updated = after.strategy_state
            assert isinstance(updated, AltBStrategyState)
            # El coordinator conserva exactamente el snapshot retornado por AltBEngine.
            reason = updated.block_reason
            blocks = audit.blocks
            if reason is not None and reason != previous.block_reason:
                blocks += (ReplayBlock(at, reason),)
            audit = replace(
                audit,
                range_start=min(audit.range_start or start, start),
                range_end=max(audit.range_end or at, at),
                candles_processed=audit.candles_processed
                + int(isinstance(event, Candle) and not duplicate),
                checkpoints_evaluated=audit.checkpoints_evaluated
                + int(isinstance(event, Candle) and not duplicate and at.minute % 15 == 0),
                gaps=gaps,
                blocks=blocks,
                rounds_opened=audit.rounds_opened
                + int(previous.round_state is None and updated.round_state is not None),
            )
            current = replace(
                current, coordinator=after, through=at, last_key=key, outputs=outputs, audit=audit
            )
        return current

    def run_sqlite(
        self,
        path: Path,
        start: datetime,
        end: datetime,
        *,
        events: Iterable[ReplayEvent] = (),
        state: ReplayState | None = None,
    ) -> ReplayState:
        """Velas con apertura en [start,end); end es cierre máximo explícito."""
        for at in (start, end):
            utc_timestamp(at, "range")
            if at.second or at.microsecond:
                raise ValueError("Rango SQLite no alineado a minuto")
        if end <= start:
            raise ValueError("Rango SQLite vacío o invertido")
        current = state if state is not None else ReplayState(config=self.config)
        require_type(current, ReplayState, "state")
        if current.sqlite_end is not None and start != current.sqlite_end:
            raise ValueError("Los segmentos SQLite deben ser contiguos")
        if current.through is not None and start < current.through:
            raise ValueError("Rango SQLite anterior al cursor Replay")
        gaps: list[Gap] = []

        def explicit_events() -> Generator[ReplayEvent]:
            for event in events:
                if isinstance(event, Candle):
                    raise ValueError("En run_sqlite las Candle proceden solo de SQLite")
                at = event_key(event)[0]
                if not start <= at <= end:
                    raise ValueError("Evento explícito fuera del rango SQLite")
                yield event

        def closed_candles(db: FuturesSQLite) -> Generator[Candle]:
            expected = start
            # read() devuelve el generador candles(); cerrar su cursor antes de SQLite.
            records = cast(Generator[Candle], db.read(self.config.symbol, start, end))
            with closing(records):
                for candle in records:
                    if candle.open_time > expected:
                        gaps.append(Gap(expected, candle.open_time))
                    if candle.close_time > end:
                        raise ValueError("Vela futura o no cerrada en el rango")
                    expected = candle.close_time
                    yield candle
            if expected < end:
                gaps.append(Gap(expected, end))

        with FuturesSQLite(path, read_only=True) as db:
            with closing(closed_candles(db)) as candles, closing(explicit_events()) as fixtures:
                with closing(_merge(candles, fixtures)) as stream:
                    result = self.run(stream, current)
        audit = result.audit
        return replace(
            result,
            through=end,
            sqlite_end=end,
            audit=replace(
                audit,
                range_start=min(audit.range_start or start, start),
                range_end=end,
                gaps=audit.gaps + tuple(gaps),
            ),
        )
