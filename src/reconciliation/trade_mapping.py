"""
Mapping from reconciler output (DeltaTrade) to the approval-layer trade record
(ProposedTrade).

This lives in its own module because the mapping is the single place where
weight metadata crosses from the reconciler into the stored approval payload.
Dropping it here is what caused executed buys to write weight=NULL into
sleeve_positions, which in turn made those positions permanently un-sellable.
"""

from src.reconciliation.delta_reconciler import DeltaTrade
from src.signals.models import ProposedTrade


def delta_trade_to_proposed(
    trade: DeltaTrade,
    proposal_prices: dict[str, float] | None = None,
    entry_prices: dict[str, float] | None = None,
) -> ProposedTrade:
    """
    Convert a DeltaTrade into a ProposedTrade, preserving weight metadata.

    current_weight / target_weight / delta_weight must survive this hop: the
    ledger updater reads them back off the stored dict after execution to
    decide what weight to record and whether a sell is a full exit.
    """
    proposal_prices = proposal_prices or {}
    entry_prices = entry_prices or {}

    target_weight = float(trade.target_weight)
    delta_weight = float(trade.weight_delta)

    return ProposedTrade(
        symbol=trade.symbol,
        side=trade.side,
        notional=float(trade.notional),
        quantity=float(trade.quantity) if trade.quantity else None,
        current_weight=target_weight - delta_weight,
        target_weight=target_weight,
        delta_weight=delta_weight,
        rationale=trade.rationale,
        proposal_price=proposal_prices.get(trade.symbol),
        bravos_entry_price=entry_prices.get(trade.symbol),
    )
