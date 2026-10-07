"""Aceptación y eventos posteriores; no presupone fills inmediatos."""

from typing import Protocol

from trading_engine.models.orders import Fill, Order, OrderIntent


class ExecutionAdapter(Protocol):
    def submit(self, intent: OrderIntent) -> Order: ...

    def poll(self) -> tuple[Order | Fill, ...]:
        """Devuelve únicamente los eventos actualmente disponibles, sin bloquear."""
        ...
