"""Disponibilidades independientes; jamás READY financiero ni liquidación certificada."""

from dataclasses import dataclass
from datetime import datetime

from .audit import FundingInventory
from .contracts import DatasetType
from .reconciliation import HistoricalReconciliation


@dataclass(frozen=True)
class HistoricalRegistryReadiness:
    trade_completeness: str
    mark_completeness: str
    funding_completeness: str
    symbol_rules: str
    historical_brackets: str
    historical_fees: str
    source_authenticity: str = "NOT_AUTHENTICATED"
    causal_availability: str = "NOT_CAUSALLY_USABLE"
    financial_adaptation: str = "NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES"
    liquidation_validation: str = "INCOMPLETE"
    datasets: str = "DATASET_NOT_PROVIDED"


def registry_readiness(
    reconciliation: HistoricalReconciliation,
    start: datetime,
    end: datetime,
    *,
    cutoff: datetime,
    funding_inventory: FundingInventory | None = None,
) -> HistoricalRegistryReadiness:
    states: list[str] = []
    for kind in (
        DatasetType.TRADE,
        DatasetType.MARK,
        DatasetType.FUNDING,
        DatasetType.SYMBOL_RULES,
        DatasetType.BRACKETS,
        DatasetType.FEES,
    ):
        try:
            report = reconciliation.evaluate(
                kind,
                start,
                end,
                cutoff=cutoff,
                funding_inventory=funding_inventory if kind is DatasetType.FUNDING else None,
            )
            state = "INCOMPLETE" if report.coverage_status == "PARTIAL" else report.coverage_status
        except ValueError as exc:
            if str(exc) != "NOT_CAUSALLY_USABLE":
                raise
            state = "NOT_CAUSALLY_USABLE"
        states.append(state)
    causal = (
        "CAUSALLY_USABLE_FOR_REVIEW"
        if all(s == "AVAILABLE_FOR_REVIEW" for s in states)
        else "NOT_CAUSALLY_USABLE"
    )
    return HistoricalRegistryReadiness(
        states[0],
        states[1],
        states[2],
        states[3],
        states[4],
        states[5],
        causal_availability=causal,
        datasets="LOCAL_DATASETS_DECLARED"
        if reconciliation.query.dataset_ids
        else "DATASET_NOT_PROVIDED",
    )
