"""Asientos, posiciones por ronda y conflictos frozen; sin cuentas ni margen."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_engine.models.enums import Side
from trading_engine.models.ids import FillId, IntentId, OrderId, RoundId
from trading_engine.models.values import Symbol
from trading_engine.replay.financial_ledger.policy import ExecutionOrigin, LedgerPolicy
from trading_engine.replay.ports import FillEvidence, ReplayFact


@dataclass(frozen=True)
class LedgerEntry:
    fill_id: FillId
    order_id: OrderId
    intent_id: IntentId
    round_id: RoundId
    symbol: Symbol
    side: Side
    quantity: Decimal
    price: Decimal
    notional_usdc: Decimal
    executed_at: datetime
    received_at: datetime
    sequence: int
    provenance: ExecutionOrigin
    cost_released_usdc: Decimal
    gross_realized_pnl_usdc: Decimal
    open_quantity_after: Decimal
    open_cost_basis_after_usdc: Decimal
    evidence: FillEvidence


@dataclass(frozen=True)
class RoundLedger:
    round_id: RoundId
    symbol: Symbol
    buy_quantity: Decimal = Decimal("0")
    sell_quantity: Decimal = Decimal("0")
    gross_buy_notional_usdc: Decimal = Decimal("0")
    gross_sell_notional_usdc: Decimal = Decimal("0")
    open_quantity: Decimal = Decimal("0")
    open_cost_basis_usdc: Decimal = Decimal("0")
    average_open_cost_usdc: Decimal | None = None
    gross_realized_pnl_usdc: Decimal = Decimal("0")
    closed_at: datetime | None = None
    reconciliation_status: str = "RECONCILED"


@dataclass(frozen=True)
class LedgerClosure:
    round_id: RoundId
    confirmed_at: datetime


@dataclass(frozen=True)
class LedgerConflict:
    detected_at: datetime
    reason: str
    observation: ReplayFact | LedgerClosure | None = None


@dataclass(frozen=True)
class LedgerState:
    policy: LedgerPolicy
    identity: str
    entries: tuple[LedgerEntry, ...] = ()
    round_ledger: tuple[RoundLedger, ...] = ()
    journal: tuple[ReplayFact | LedgerClosure, ...] = ()
    conflicts: tuple[LedgerConflict, ...] = ()
    last_at: datetime | None = None

    @property
    def reconciliation_status(self) -> str:
        return "RECONCILIATION_REQUIRED" if self.conflicts else "RECONCILED"

    @property
    def processed_fill_ids(self) -> tuple[FillId, ...]:
        return tuple(e.fill_id for e in self.entries)


@dataclass(frozen=True)
class LedgerReport:
    identity: str
    gross_realized_pnl_usdc: Decimal
    open_quantity: Decimal
    open_cost_basis_usdc: Decimal
    gross_buy_notional_usdc: Decimal
    gross_sell_notional_usdc: Decimal
    round_ledger: tuple[RoundLedger, ...]
    entries: tuple[LedgerEntry, ...]
    processed_fill_ids: tuple[FillId, ...]
    reconciliation_status: str
    conflicts: tuple[LedgerConflict, ...]
    provenances: tuple[ExecutionOrigin, ...]
    modeled_results: bool
    net_realized_pnl: str = "NOT_AVAILABLE"
    unrealized_pnl: str = "NOT_AVAILABLE"
    account_equity: str = "NOT_AVAILABLE"
    drawdown: str = "NOT_AVAILABLE"
    funding: str = "NOT_AVAILABLE"
    fees: str = "NOT_AVAILABLE"
    liquidation_price: str = "NOT_AVAILABLE"
