"""Cancelación explícita sin efectos externos ni confirmaciones artificiales."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from typing import cast

import pytest
from test_orchestration import CannedStrategy

from trading_engine.models.decisions import StrategyResult
from trading_engine.models.market import Quote
from trading_engine.models.orders import OrderCancelRequest, OrderIntent
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinationResult, CoordinatorState


def test_request_is_frozen_and_correlates_by_intent(intent: OrderIntent) -> None:
    request = OrderCancelRequest(intent, intent.created_at)
    assert request.intent_id == intent.intent_id
    with pytest.raises(FrozenInstanceError):
        request.intent = intent  # type: ignore[misc]
    with pytest.raises(ValueError):
        replace(request, created_at=intent.created_at - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        replace(request, created_at=intent.created_at.replace(tzinfo=None))
    with pytest.raises(TypeError):
        OrderCancelRequest(cast(OrderIntent, "invalid"), intent.created_at)


def test_result_and_coordinator_expose_requests_without_sending_them(
    intent: OrderIntent, quote: Quote
) -> None:
    request = OrderCancelRequest(intent, quote.timestamp)
    result = StrategyResult(cancel_requests=(request,))
    strategy = CannedStrategy((result,))
    coordinated = StrategyCoordinator(strategy).on_quote(CoordinatorState(), quote)
    assert coordinated.cancel_requests == (request,)
    assert not coordinated.order_intents and not coordinated.signals
    assert coordinated.state == CoordinatorState() and len(strategy.calls) == 1
    assert not StrategyResult().cancel_requests


@pytest.mark.parametrize("value", [[], None, ("wrong",), (1,)])
def test_rejects_wrong_cancellation_containers(intent: OrderIntent, value: object) -> None:
    for factory in (
        lambda: StrategyResult(cancel_requests=cast(tuple[OrderCancelRequest, ...], value)),
        lambda: CoordinationResult(
            (), (), CoordinatorState(), cast(tuple[OrderCancelRequest, ...], value)
        ),
    ):
        with pytest.raises(TypeError):
            factory()


def test_duplicate_request_and_submit_cancel_conflict_rejected(intent: OrderIntent) -> None:
    request = OrderCancelRequest(intent, intent.created_at)
    with pytest.raises(ValueError, match="duplicada"):
        StrategyResult(cancel_requests=(request, request))
    with pytest.raises(ValueError, match="misma intención"):
        StrategyResult(order_intents=(intent,), cancel_requests=(request,))
    with pytest.raises(ValueError, match="duplicada"):
        CoordinationResult((), (), CoordinatorState(), (request, request))


def test_causal_guard_covers_cancellation_outputs(intent: OrderIntent, quote: Quote) -> None:
    request = OrderCancelRequest(intent, quote.timestamp)
    strategy = CannedStrategy((StrategyResult(cancel_requests=(request,)),))
    with pytest.raises(ValueError, match="OrderCancelRequest.created_at"):
        StrategyCoordinator(strategy).on_quote(
            CoordinatorState(),
            replace(quote, timestamp=quote.timestamp + timedelta(microseconds=1)),
        )


def test_coordinated_result_rejects_submit_cancel_conflict(intent: OrderIntent) -> None:
    request = OrderCancelRequest(intent, intent.created_at)
    with pytest.raises(ValueError, match="misma intención"):
        CoordinationResult((), (intent,), CoordinatorState(), (request,))
