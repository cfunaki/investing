"""
Delta-only reconciliation for sleeve positions.

Instead of reconciling the entire portfolio, this only generates trades
for specific symbols that have changed (new positions, weight changes, exits).

Key principles:
- Only touch symbols mentioned in the trigger
- Don't rebalance other positions
- Use $X per weight unit sizing
- Track positions in virtual ledger (sleeve_positions table)
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from src.db.models import Sleeve, SleevePosition
from src.db.repositories.sleeve_position_repository import sleeve_position_repository
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context

logger = structlog.get_logger(__name__)


@dataclass
class WeightChange:
    """Represents a weight change for a symbol."""

    symbol: str
    old_weight: Decimal
    new_weight: Decimal
    action: str  # 'enter', 'exit', 'increase', 'decrease', 'unchanged'

    @property
    def weight_delta(self) -> Decimal:
        return self.new_weight - self.old_weight


@dataclass
class DeltaTrade:
    """A single trade to execute based on weight change."""

    symbol: str
    side: str  # 'buy' or 'sell'
    notional: Decimal  # Dollar amount to trade
    weight_delta: Decimal  # Weight units changed
    target_weight: Decimal  # Final weight after this trade
    rationale: str


@dataclass
class DeltaReconciliationResult:
    """Result of delta reconciliation."""

    success: bool
    trades: list[DeltaTrade]
    weight_changes: list[WeightChange]
    total_buy: Decimal
    total_sell: Decimal
    error: str | None = None


class DeltaReconciler:
    """
    Calculates delta trades based on weight changes.

    Given:
    - Current sleeve positions (from virtual ledger)
    - New target weights (from Bravos scrape or other source)

    Produces:
    - List of trades for only the symbols that changed
    """

    def __init__(self, unit_size: Decimal = Decimal("500.00")):
        self.unit_size = unit_size

    async def reconcile(
        self,
        sleeve_id: UUID,
        new_weights: dict[str, Decimal],
        prices: dict[str, Decimal] | None = None,
    ) -> DeltaReconciliationResult:
        """
        Calculate delta trades for weight changes.

        Args:
            sleeve_id: The sleeve to reconcile
            new_weights: Dict of symbol -> new weight (raw units, not %)
            prices: Optional dict of symbol -> current price (for share calculation)

        Returns:
            DeltaReconciliationResult with trades to execute
        """
        log = logger.bind(sleeve_id=str(sleeve_id), symbols=list(new_weights.keys()))

        try:
            async with get_db_context() as db:
                # Get current positions from virtual ledger
                current_positions = await sleeve_position_repository.get_position_map(
                    db, sleeve_id
                )

                # Get sleeve config for unit_size
                sleeve = await sleeve_repository.get_by_id(db, sleeve_id)
                if sleeve and sleeve.unit_size:
                    self.unit_size = sleeve.unit_size

            log.info(
                "delta_reconcile_start",
                current_positions=len(current_positions),
                new_weights=len(new_weights),
                unit_size=float(self.unit_size),
            )

            # Calculate weight changes
            weight_changes = self._calculate_weight_changes(
                current_positions, new_weights
            )

            # Generate trades for changes
            trades = self._generate_trades(weight_changes)

            total_buy = sum(t.notional for t in trades if t.side == "buy")
            total_sell = sum(t.notional for t in trades if t.side == "sell")

            log.info(
                "delta_reconcile_complete",
                trade_count=len(trades),
                total_buy=float(total_buy),
                total_sell=float(total_sell),
            )

            return DeltaReconciliationResult(
                success=True,
                trades=trades,
                weight_changes=weight_changes,
                total_buy=total_buy,
                total_sell=total_sell,
            )

        except Exception as e:
            log.exception("delta_reconcile_failed", error=str(e))
            return DeltaReconciliationResult(
                success=False,
                trades=[],
                weight_changes=[],
                total_buy=Decimal(0),
                total_sell=Decimal(0),
                error=str(e),
            )

    def _calculate_weight_changes(
        self,
        current_positions: dict[str, SleevePosition],
        new_weights: dict[str, Decimal],
    ) -> list[WeightChange]:
        """Compare current positions to new weights and identify changes."""
        changes = []

        # All symbols we need to consider
        all_symbols = set(current_positions.keys()) | set(new_weights.keys())

        for symbol in all_symbols:
            current_pos = current_positions.get(symbol)
            old_weight = current_pos.weight if current_pos else Decimal(0)
            new_weight = new_weights.get(symbol, Decimal(0))

            # Determine action
            if old_weight == 0 and new_weight > 0:
                action = "enter"
            elif old_weight > 0 and new_weight == 0:
                action = "exit"
            elif new_weight > old_weight:
                action = "increase"
            elif new_weight < old_weight:
                action = "decrease"
            else:
                action = "unchanged"

            # Only include if there's an actual change
            if action != "unchanged":
                changes.append(
                    WeightChange(
                        symbol=symbol,
                        old_weight=old_weight or Decimal(0),
                        new_weight=new_weight,
                        action=action,
                    )
                )

        return changes

    def _generate_trades(self, weight_changes: list[WeightChange]) -> list[DeltaTrade]:
        """Generate trades from weight changes."""
        trades = []

        for change in weight_changes:
            if change.action == "unchanged":
                continue

            delta = abs(change.weight_delta)
            notional = delta * self.unit_size

            if change.action == "enter":
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="buy",
                        notional=notional,
                        weight_delta=change.new_weight,
                        target_weight=change.new_weight,
                        rationale=f"New position: weight {change.new_weight}",
                    )
                )
            elif change.action == "exit":
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="sell",
                        notional=notional,  # This will be overridden to sell all
                        weight_delta=-change.old_weight,
                        target_weight=Decimal(0),
                        rationale=f"Exit position: was weight {change.old_weight}",
                    )
                )
            elif change.action == "increase":
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="buy",
                        notional=notional,
                        weight_delta=change.weight_delta,
                        target_weight=change.new_weight,
                        rationale=f"Increase weight: {change.old_weight} → {change.new_weight}",
                    )
                )
            elif change.action == "decrease":
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="sell",
                        notional=notional,
                        weight_delta=change.weight_delta,
                        target_weight=change.new_weight,
                        rationale=f"Decrease weight: {change.old_weight} → {change.new_weight}",
                    )
                )

        return trades

    async def update_ledger_after_trades(
        self,
        sleeve_id: UUID,
        executed_trades: list[dict[str, Any]],
    ) -> None:
        """
        Update the virtual ledger after trades are executed.

        Args:
            sleeve_id: The sleeve ID
            executed_trades: List of executed trades with:
                - symbol: str
                - side: 'buy' or 'sell'
                - shares: Decimal (actual shares traded)
                - notional: Decimal (actual dollar amount)
                - weight: Decimal (new weight for this symbol)
        """
        log = logger.bind(sleeve_id=str(sleeve_id), trade_count=len(executed_trades))

        async with get_db_context() as db:
            for trade in executed_trades:
                symbol = trade["symbol"]
                side = trade["side"]
                shares = Decimal(str(trade["shares"]))
                weight = Decimal(str(trade.get("weight", 0)))
                cost = Decimal(str(trade.get("notional", 0)))

                if side == "buy":
                    await sleeve_position_repository.add_shares(
                        db,
                        sleeve_id=sleeve_id,
                        symbol=symbol,
                        shares_delta=shares,
                        weight=weight,
                        cost_delta=cost,
                    )
                    log.info("ledger_added_shares", symbol=symbol, shares=float(shares))

                elif side == "sell":
                    if weight == 0:
                        # Full exit
                        await sleeve_position_repository.delete_position(
                            db, sleeve_id, symbol
                        )
                        log.info("ledger_removed_position", symbol=symbol)
                    else:
                        # Partial sell
                        await sleeve_position_repository.remove_shares(
                            db, sleeve_id, symbol, shares
                        )
                        # Update weight
                        pos = await sleeve_position_repository.get_position(
                            db, sleeve_id, symbol
                        )
                        if pos:
                            await sleeve_position_repository.update_position(
                                db, pos, weight=weight
                            )
                        log.info(
                            "ledger_reduced_shares", symbol=symbol, shares=float(shares)
                        )

            await db.commit()
            log.info("ledger_updated")


def parse_bravos_weights(bravos_data: dict[str, Any]) -> dict[str, Decimal]:
    """
    Parse Bravos scrape data to extract current weights.

    Args:
        bravos_data: The bravos_trades.json data structure

    Returns:
        Dict of symbol -> weight (Decimal)
    """
    weights = {}
    trades = bravos_data.get("trades", {})

    for symbol, trade_info in trades.items():
        if trade_info.get("status") == "active":
            weight = trade_info.get("currentWeight", 0)
            if weight and weight > 0:
                weights[symbol.upper()] = Decimal(str(weight))

    return weights


# Singleton instance
_reconciler: DeltaReconciler | None = None


def get_delta_reconciler() -> DeltaReconciler:
    """Get the delta reconciler singleton."""
    global _reconciler
    if _reconciler is None:
        _reconciler = DeltaReconciler()
    return _reconciler
