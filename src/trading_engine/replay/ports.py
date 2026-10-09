"""Puertos offline explícitos. Los fixtures nunca se presentan como Binance real."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol, runtime_checkable

from trading_engine.models._validation import nonblank, require_type
from trading_engine.models.market import Quote
from trading_engine.models.orders import Fill, Order
from trading_engine.models.values import Symbol
from trading_engine.replay.encoding import encoded
from trading_engine.utils.time import require_utc


@dataclass(frozen=True)
class QuoteEvidence:
    quote: Quote
    provenance: str

    def __post_init__(self) -> None:
        require_type(self.quote, Quote, "quote")
        nonblank(self.provenance, "provenance")


class QuoteSource(Protocol):
    @property
    def identity(self) -> str: ...

    def at(self, symbol: Symbol, at: datetime) -> QuoteEvidence | None:
        """Únicamente evidencia disponible en at; sin construirla de OHLC."""
        ...


@runtime_checkable
class ExecutionQuoteSource(QuoteSource, Protocol):
    """Extensión opcional para fuentes modeladas vinculadas a una sesión de datos."""

    @property
    def execution_identity(self) -> str: ...

    @property
    def history_session(self) -> object: ...


@dataclass(frozen=True)
class NoQuotes:
    identity: str = "no-quotes-v1"

    def at(self, symbol: Symbol, at: datetime) -> QuoteEvidence | None:
        return None


@dataclass(frozen=True)
class FixtureQuotes:
    quotes: tuple[Quote, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.quotes, tuple, "quotes")
        for quote in self.quotes:
            require_type(quote, Quote, "quote")
        keys = [(q.symbol.value, q.timestamp) for q in self.quotes]
        if len(set(keys)) != len(keys):
            raise ValueError("Quotes contradictorios o duplicados en el mismo instante")
        object.__setattr__(
            self, "quotes", tuple(sorted(self.quotes, key=lambda q: (q.timestamp, q.symbol.value)))
        )

    @property
    def identity(self) -> str:
        return "synthetic-fixture-quotes:" + sha256(encoded(self.quotes)).hexdigest()

    def at(self, symbol: Symbol, at: datetime) -> QuoteEvidence | None:
        quote = next((q for q in self.quotes if q.symbol == symbol and q.timestamp == at), None)
        return None if quote is None else QuoteEvidence(quote, "SYNTHETIC_FIXTURE")


@dataclass(frozen=True)
class EntryControl:
    allow_entries: bool

    def __post_init__(self) -> None:
        require_type(self.allow_entries, bool, "allow_entries")


@dataclass(frozen=True)
class FillEvidence:
    fill: Fill
    order: Order

    def __post_init__(self) -> None:
        require_type(self.fill, Fill, "fill")
        require_type(self.order, Order, "order")
        if self.fill.order_id != self.order.order_id:
            raise ValueError("Fill y Order incompatibles")


@dataclass(frozen=True)
class ReplayFact:
    received_at: datetime
    payload: EntryControl | Order | FillEvidence
    sequence: int
    provenance: str = "SYNTHETIC_FIXTURE"

    def __post_init__(self) -> None:
        require_utc(self.received_at)
        if not isinstance(self.payload, (EntryControl, Order, FillEvidence)):
            raise TypeError("Hecho de replay inválido")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence requiere entero no negativo")
        nonblank(self.provenance, "provenance")
        times: tuple[datetime, ...] = ()
        if isinstance(self.payload, Order):
            times = (self.payload.updated_at,)
        elif isinstance(self.payload, FillEvidence):
            times = (self.payload.fill.timestamp, self.payload.order.updated_at)
        if any(t > self.received_at for t in times):
            raise ValueError("Hecho futuro respecto a received_at")

    @property
    def priority(self) -> int:
        if isinstance(self.payload, EntryControl):
            return 0
        return 1 if isinstance(self.payload, Order) else 2

    @property
    def key(self) -> tuple[datetime, int, int]:
        return self.received_at, self.priority, self.sequence


class FactSource(Protocol):
    @property
    def identity(self) -> str: ...

    def read(self, cursor: int, until: datetime) -> Iterator[ReplayFact]:
        """Cursor ordinal estable; orden (received_at, prioridad, sequence)."""
        ...


@dataclass(frozen=True)
class FixtureFacts:
    facts: tuple[ReplayFact, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.facts, tuple, "facts")
        for fact in self.facts:
            require_type(fact, ReplayFact, "fact")
        if len({f.sequence for f in self.facts}) != len(self.facts):
            raise ValueError("sequence duplicada")
        object.__setattr__(self, "facts", tuple(sorted(self.facts, key=lambda f: f.key)))

    @property
    def identity(self) -> str:
        return "synthetic-fixture-facts:" + sha256(encoded(self.facts)).hexdigest()

    def read(self, cursor: int, until: datetime) -> Iterator[ReplayFact]:
        for fact in self.facts[cursor:]:
            if fact.received_at > until:
                break
            yield fact
