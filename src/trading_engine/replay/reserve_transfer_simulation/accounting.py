"""Conservación entre partidas; consume wallet y margen, no recalcula PnL."""

from dataclasses import replace
from decimal import Decimal, localcontext

from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.replay.isolated_margin_risk.models import RiskContext, RiskEvaluation
from trading_engine.replay.reserve_transfer_simulation.models import CapitalPartition, ReserveState
from trading_engine.replay.reserve_transfer_simulation.orders import money

ZERO = Decimal(0)


def declared_partition(state: ReserveState) -> CapitalPartition:
    capital = state.capital
    if capital is None:
        unavailable = money(None, "CAPITAL_PARTITION_MISSING")
        return CapitalPartition(
            unavailable,
            unavailable,
            unavailable,
            unavailable,
            unavailable,
            ZERO,
            "PARTITION_NOT_RECONCILED",
        )
    with localcontext(ledger_context()):
        return CapitalPartition(
            money(capital.account_wallet_usdc),
            money(capital.free_balance_usdc),
            money(sum((v for _, v in capital.isolated_by_round), ZERO)),
            money(sum((v for _, v in capital.order_holds), ZERO)),
            money(ZERO),
            sum((v for _, v in capital.other_buckets), ZERO),
            "RECONCILED",
        )


def reconcile(
    state: ReserveState, context: RiskContext, evaluation: RiskEvaluation
) -> ReserveState:
    with localcontext(ledger_context()):
        isolated = [p.isolated_wallet_balance.value for p in evaluation.positions]
        holds = [r.hold.value for r in state.reservations]
        wallet = context.financial.wallet.value
        pending = sum((r.held_usdc for r in state.transfers), ZERO)
        reasons = []
        capital = state.capital
        if (
            capital is None
            or capital.stamp.received_at > context.at
            or capital.stamp.reference.provenance.value == "UNKNOWN"
        ):
            reasons.append("CAPITAL_PARTITION_MISSING_OR_UNKNOWN")
        if wallet is None or any(v is None for v in isolated):
            reasons.append("ALLOCATION_UNKNOWN")
        if any(v is None for v in holds):
            reasons.append("PENDING_ORDER_HOLDS")
        if context.ledger.conflicts or state.conflicts:
            reasons.append("CONFLICTS")
        if reasons:
            partition = replace(
                state.partition,
                account_wallet=context.financial.wallet,
                free_balance=money(None, ";".join(reasons)),
                isolated_wallet_allocated=money(None)
                if any(v is None for v in isolated)
                else money(sum((v for v in isolated if v is not None), ZERO)),
                pending_order_holds=money(None)
                if any(v is None for v in holds)
                else money(sum((v for v in holds if v is not None), ZERO)),
                pending_transfer_holds=money(pending),
                status="PARTITION_NOT_RECONCILED",
            )
            return replace(state, partition=partition)
        assert wallet is not None and capital is not None
        allocated = sum((v for v in isolated if v is not None), ZERO)
        order_holds = sum((v for v in holds if v is not None), ZERO)
        previous = state.settled_partition
        assert previous is not None
        if not state.initialized:
            expected = declared_partition(state)
            if (
                wallet != expected.account_wallet.value
                or allocated != expected.isolated_wallet_allocated.value
                or order_holds != expected.pending_order_holds.value
                or tuple(e.fill_id for e in context.ledger.entries) != capital.fill_ids
                or tuple(
                    (p.round_id, p.isolated_wallet_balance.value) for p in evaluation.positions
                )
                != capital.isolated_by_round
                or tuple(
                    (r.order_id, r.hold.value) for r in state.reservations if r.hold.value != 0
                )
                != tuple(sorted(capital.order_holds, key=lambda pair: pair[0].int))
            ):
                return replace(
                    state,
                    partition=CapitalPartition(
                        context.financial.wallet,
                        money(None, "INITIAL_PARTITION_EVIDENCE_MISMATCH"),
                        money(allocated),
                        money(order_holds),
                        money(pending),
                        previous.other_accounted_buckets,
                        "PARTITION_NOT_RECONCILED",
                    ),
                )
        assert previous.account_wallet.value is not None
        assert previous.free_balance.value is not None
        assert previous.isolated_wallet_allocated.value is not None
        assert previous.pending_order_holds.value is not None
        assert previous.pending_transfer_holds.value is not None
        # Diferencias económicas reconocidas por 4A-3B/4A-4A; no partida residual.
        # Reconciliar desde saldos conocidos evita acumular redondeos de deltas.
        # Sólo se llega aquí con capital inicial conciliado y TODAS las partidas
        # reconocidas; nunca se crea una partida residual para datos ausentes.
        committed = allocated + order_holds + pending + previous.other_accounted_buckets
        free = wallet - committed
        if free < 0 or allocated < 0:
            return replace(
                state,
                partition=CapitalPartition(
                    context.financial.wallet,
                    money(None, "INSUFFICIENT_FREE_BALANCE_OR_NEGATIVE_ALLOCATION"),
                    money(allocated),
                    money(order_holds),
                    money(pending),
                    previous.other_accounted_buckets,
                    "PARTITION_NOT_RECONCILED",
                ),
            )
        partition = CapitalPartition(
            context.financial.wallet,
            money(free),
            money(allocated),
            money(order_holds),
            money(pending),
            previous.other_accounted_buckets,
            "RECONCILED",
        )
        if wallet != free + committed:
            raise ValueError("Divergencia de particiones")
        return replace(
            state,
            partition=partition,
            settled_partition=partition,
            initialized=True,
            processed_fill_ids=tuple(e.fill_id for e in context.ledger.entries),
        )


def hold_transfer(state: ReserveState, amount: Decimal) -> ReserveState:
    with localcontext(ledger_context()):
        p = state.partition
        if (
            p.status != "RECONCILED"
            or p.free_balance.value is None
            or p.pending_transfer_holds.value is None
        ):
            raise ValueError("PARTITION_NOT_RECONCILED")
        if amount > p.free_balance.value or amount <= 0:
            raise ValueError("INSUFFICIENT_FREE_BALANCE")
        updated = replace(
            p,
            free_balance=money(p.free_balance.value - amount),
            pending_transfer_holds=money(p.pending_transfer_holds.value + amount),
        )
        return replace(state, partition=updated, settled_partition=updated)


def release_transfer(state: ReserveState, amount: Decimal, *, confirmed: bool) -> ReserveState:
    with localcontext(ledger_context()):
        # También puede liberar una retención conocida mientras otra partida sea desconocida.
        p = state.settled_partition
        assert p is not None and p.pending_transfer_holds.value is not None
        assert p.free_balance.value is not None and p.isolated_wallet_allocated.value is not None
        if amount <= 0 or amount > p.pending_transfer_holds.value:
            raise ValueError("Hold de transferencia insuficiente")
        updated = replace(
            p,
            pending_transfer_holds=money(p.pending_transfer_holds.value - amount),
            free_balance=p.free_balance if confirmed else money(p.free_balance.value + amount),
            isolated_wallet_allocated=money(p.isolated_wallet_allocated.value + amount)
            if confirmed
            else p.isolated_wallet_allocated,
        )
        return replace(
            state,
            partition=updated if state.partition.status == "RECONCILED" else state.partition,
            settled_partition=updated,
        )
