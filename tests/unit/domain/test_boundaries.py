import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from trading_engine.models.account import BalanceSnapshot, Position
from trading_engine.models.ids import FillId, SignalId
from trading_engine.models.market import Candle
from trading_engine.models.orders import Fill, Order, OrderIntent
from trading_engine.models.rounds import Round
from trading_engine.models.signals import Signal
from trading_engine.models.values import Price, Quantity


def test_domain_and_protocols_do_not_import_external_implementations() -> None:
    source = Path(__file__).resolve().parents[3] / "src" / "trading_engine"
    modules = list((source / "models").glob("*.py"))
    modules.extend(source.glob("*/contracts.py"))
    assert modules and (source / "models").is_dir()
    for module in modules:
        for node in ast.walk(ast.parse(module.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"Import relativo no auditado: {module}"
                names = [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                if name.startswith("trading_engine."):
                    assert name.startswith(("trading_engine.models.", "trading_engine.utils.")), (
                        module
                    )


def test_all_snapshots_reject_non_utc_offsets(
    candle: Candle,
    intent: OrderIntent,
    order: Order,
    position: Position,
    balance: BalanceSnapshot,
    round_state: Round,
) -> None:
    non_utc = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-6)))
    signal = Signal(SignalId(UUID(int=5)), candle.symbol, candle.open_time, "observation")
    fill = Fill(
        FillId(UUID(int=6)),
        order.order_id,
        order.created_at,
        Price(Decimal("2")),
        Quantity(Decimal("1")),
    )
    for snapshot, field in (
        (candle, "open_time"),
        (candle, "close_time"),
        (intent, "created_at"),
        (intent, "not_before"),
        (order, "created_at"),
        (order, "updated_at"),
        (position, "timestamp"),
        (balance, "timestamp"),
        (round_state, "created_at"),
        (round_state, "opened_at"),
        (round_state, "closed_at"),
        (signal, "timestamp"),
        (fill, "timestamp"),
    ):
        with pytest.raises(ValueError):
            replace(snapshot, **{field: non_utc})  # type: ignore[arg-type]
