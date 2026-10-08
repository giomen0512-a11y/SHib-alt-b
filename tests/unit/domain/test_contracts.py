from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from trading_engine.execution.contracts import ExecutionAdapter
from trading_engine.market_data.contracts import MarketDataProvider
from trading_engine.models.account import BalanceSnapshot, Position
from trading_engine.models.actions import ScheduledAction
from trading_engine.models.decisions import RiskDecision, StrategyResult
from trading_engine.models.enums import OrderStatus
from trading_engine.models.ids import FillId, SignalId
from trading_engine.models.market import Candle, Quote
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.signals import Signal
from trading_engine.models.values import Price, Quantity, Symbol
from trading_engine.risk.contracts import RiskManager
from trading_engine.strategy.contracts import StrategyEngine


class FakeMarket:
    def __init__(self, candle: Candle) -> None:
        self.candle = candle

    def candles(self, symbol: Symbol) -> Iterator[Candle]:
        assert symbol == self.candle.symbol
        return iter((self.candle,))


class FakeStrategy:
    def __init__(self, response: StrategyResult) -> None:
        self.response = response

    def on_candle(
        self, candle: Candle, position: Position | None, round_state: Round | None
    ) -> StrategyResult:
        return self.response

    def on_quote(
        self, quote: Quote, position: Position | None, round_state: Round | None
    ) -> StrategyResult:
        return self.response

    def on_order(
        self, order: Order, position: Position | None, round_state: Round | None
    ) -> StrategyResult:
        return self.response

    def on_fill(
        self, fill: Fill, order: Order, position: Position | None, round_state: Round | None
    ) -> StrategyResult:
        return self.response

    def on_scheduled_action(
        self,
        action: ScheduledAction,
        quote: Quote,
        position: Position | None,
        round_state: Round | None,
    ) -> StrategyResult:
        return self.response


class FakeRisk:
    def __init__(self, response: RiskDecision) -> None:
        self.response = response

    def evaluate(
        self, intent: OrderIntent, balance: BalanceSnapshot, position: Position | None
    ) -> RiskDecision:
        return self.response


class FakeExecution:
    def __init__(self, order: Order, events: tuple[Order | Fill, ...]) -> None:
        self.order = order
        self.events = events

    def submit(self, intent: OrderIntent) -> Order:
        assert intent == self.order.intent
        return self.order

    def poll(self) -> tuple[Order | Fill, ...]:
        events = self.events
        self.events = ()
        return events


def test_protocols_accept_structural_fakes_and_partial_updates(
    candle: Candle, intent: OrderIntent, order: Order, balance: BalanceSnapshot
) -> None:
    market: MarketDataProvider = FakeMarket(candle)
    signal = Signal(SignalId(UUID(int=5)), intent.symbol, intent.created_at, "observation")
    response = StrategyResult(signals=(signal,), order_intents=(intent,))
    strategy: StrategyEngine = FakeStrategy(response)
    risk: RiskManager = FakeRisk(RiskDecision(True))
    partial = replace(order, status=OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal("1"))
    fill = Fill(
        FillId(UUID(int=4)),
        order.order_id,
        order.created_at,
        Price(Decimal("2")),
        Quantity(Decimal("1")),
    )
    execution: ExecutionAdapter = FakeExecution(order, (fill, partial))
    observed = next(market.candles(candle.symbol))
    assert strategy.on_candle(observed, None, None) == response
    assert risk.evaluate(intent, balance, None).approved
    assert execution.submit(intent).filled_quantity == 0
    assert execution.poll() == (fill, partial)
    assert execution.poll() == ()


def test_contracts_support_no_intent_and_explicit_rejection(
    candle: Candle,
    intent: OrderIntent,
    balance: BalanceSnapshot,
    position: Position,
    round_state: Round,
) -> None:
    strategy: StrategyEngine = FakeStrategy(StrategyResult())
    risk: RiskManager = FakeRisk(RiskDecision(False, "Canned rejection"))
    assert strategy.on_candle(candle, position, round_state) == StrategyResult()
    assert not risk.evaluate(intent, balance, position).approved


def test_strategy_protocol_accepts_all_event_handlers(
    candle: Candle,
    quote: Quote,
    order: Order,
    fill: Fill,
    action: ScheduledAction,
    position: Position,
    round_state: Round,
) -> None:
    response = StrategyResult(scheduled_actions=(action,))
    strategy: StrategyEngine = FakeStrategy(response)
    assert strategy.on_candle(candle, position, round_state) is response
    assert strategy.on_quote(quote, position, round_state) is response
    assert strategy.on_order(order, position, round_state) is response
    assert strategy.on_fill(fill, order, position, round_state) is response
    assert strategy.on_scheduled_action(action, quote, position, round_state) is response
