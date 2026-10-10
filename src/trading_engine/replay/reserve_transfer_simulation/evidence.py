"""Capital declarado y acciones de fixture; sin acceso a balances o APIs reales."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from hashlib import sha256
from uuid import UUID

from trading_engine.models._validation import finite_decimal, nonblank, require_type
from trading_engine.models.ids import FillId, OrderId, RoundId
from trading_engine.models.values import Price
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.financial_performance.evidence import Origin
from trading_engine.replay.isolated_margin_risk.evidence import RiskStamp


@dataclass(frozen=True)
class CapitalPartitionEvidence:
    stamp: RiskStamp
    account_wallet_usdc: Decimal
    free_balance_usdc: Decimal
    isolated_by_round: tuple[tuple[RoundId, Decimal], ...]
    order_holds: tuple[tuple[OrderId, Decimal], ...]
    other_buckets: tuple[tuple[str, Decimal], ...]
    fill_ids: tuple[FillId, ...]

    def __post_init__(self) -> None:
        require_type(self.stamp, RiskStamp, "stamp")
        finite_decimal(self.account_wallet_usdc, "account_wallet", nonnegative=True)
        finite_decimal(self.free_balance_usdc, "free_balance", nonnegative=True)
        for name in ("isolated_by_round", "order_holds", "other_buckets", "fill_ids"):
            require_type(getattr(self, name), tuple, name)
        for pairs in (self.isolated_by_round, self.order_holds, self.other_buckets):
            for key, amount in pairs:
                if pairs is self.other_buckets:
                    if not isinstance(key, str):
                        raise TypeError("Bucket requiere nombre")
                    nonblank(key, "bucket")
                else:
                    require_type(key, UUID, "round/order id")
                finite_decimal(amount, "partition amount", nonnegative=True)
            if len({key for key, _ in pairs}) != len(pairs):
                raise ValueError("Particiones duplicadas")
        for fill_id in self.fill_ids:
            require_type(fill_id, UUID, "fill_id")
        if len(set(self.fill_ids)) != len(self.fill_ids):
            raise ValueError("FillIds iniciales duplicados")
        with localcontext(ledger_context()):
            total = self.free_balance_usdc + sum(
                (
                    amount
                    for pairs in (self.isolated_by_round, self.order_holds, self.other_buckets)
                    for _, amount in pairs
                ),
                Decimal(0),
            )
            if total != self.account_wallet_usdc:
                raise ValueError("PARTITION_NOT_RECONCILED: capital inicial diverge")


@dataclass(frozen=True)
class OrderPriceEvidence:
    stamp: RiskStamp
    order_id: OrderId
    price: Price
    freshness_limit_ms: int

    def __post_init__(self) -> None:
        require_type(self.stamp, RiskStamp, "stamp")
        require_type(self.order_id, UUID, "order_id")
        require_type(self.price, Price, "price")
        if type(self.freshness_limit_ms) is not int or self.freshness_limit_ms < 0:
            raise ValueError("Freshness explícita requiere int no negativo")


@dataclass(frozen=True)
class TransferCommandEvidence:
    stamp: RiskStamp
    request_id: str
    round_id: RoundId
    kind: str
    amount_usdc: Decimal | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        require_type(self.stamp, RiskStamp, "stamp")
        require_type(self.round_id, UUID, "round_id")
        nonblank(self.request_id, "request_id")
        if self.kind not in {"PROPOSE", "CONFIRM", "REJECT", "EXPIRE"}:
            raise ValueError("Acción de transferencia inválida")
        if self.kind in {"PROPOSE", "CONFIRM"}:
            if self.amount_usdc is None:
                raise ValueError("Importe obligatorio")
            finite_decimal(self.amount_usdc, "amount", nonnegative=True)
            assert self.amount_usdc is not None
            if self.amount_usdc <= 0:
                raise ValueError("Importe positivo obligatorio")
        elif self.amount_usdc is not None or self.reason is None:
            raise ValueError("Rechazo/vencimiento requiere razón sin importe")
        if self.reason is not None:
            nonblank(self.reason, "reason")


ReserveEvent = OrderPriceEvidence | TransferCommandEvidence


@dataclass(frozen=True)
class FixtureReserveSource:
    events: tuple[ReserveEvent, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.events, tuple, "events")
        for event in self.events:
            if not isinstance(event, (OrderPriceEvidence, TransferCommandEvidence)):
                raise TypeError("Fuente de reservas incompatible")
            if event.stamp.reference.provenance is not Origin.FIXTURE:
                raise ValueError("Fuente fixture requiere SYNTHETIC_FIXTURE")
        if len({e.stamp.sequence for e in self.events}) != len(self.events):
            raise ValueError("Secuencias de reserva duplicadas")
        object.__setattr__(
            self,
            "events",
            tuple(sorted(self.events, key=lambda e: (e.stamp.received_at, e.stamp.sequence))),
        )

    @property
    def identity(self) -> str:
        return "fixture-reserves-v1:" + sha256(encoded(self.events)).hexdigest()

    def read(self, cursor: int, until: datetime) -> Iterator[ReserveEvent]:
        for event in self.events[cursor:]:
            if event.stamp.received_at > until:
                break
            yield event
