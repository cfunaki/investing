"""
Buffett sleeve signal processor.

Processes new 13F filings:
1. Detects new filing via SEC EDGAR
2. Parses holdings and calculates target allocations
3. Reconciles against current Robinhood holdings
4. Sends proposed trades to Telegram for approval
5. Executes trades on approval

This integrates the Buffett adapter with the approval workflow.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog

from src.adapters.buffett_13f import Buffett13FAdapter
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context
from src.signals.buffett_detector import Buffett13FDetector, get_buffett_detector
from src.signals.models import (
    ProposedTrade,
    ReconciliationPlan,
    ReconciliationResult,
)

logger = structlog.get_logger(__name__)

# Paths
HOLDINGS_PATH = Path("data/processed/robinhood_holdings.json")
RECONCILIATION_PATH = Path("data/processed/buffett_reconciliation.json")


@dataclass
class BuffettProcessingResult:
    """Result from processing a Buffett signal."""

    success: bool
    new_filing: bool = False
    accession_number: str | None = None
    report_date: datetime | None = None
    trade_count: int = 0
    total_buy: float = 0.0
    total_sell: float = 0.0
    approval_sent: bool = False
    error: str | None = None


class BuffettSignalProcessor:
    """
    Processes Buffett sleeve signals through the approval workflow.
    """

    def __init__(
        self,
        detector: Buffett13FDetector | None = None,
        adapter: Buffett13FAdapter | None = None,
    ):
        self.detector = detector or get_buffett_detector()
        self.adapter = adapter or Buffett13FAdapter()

    async def check_and_process(
        self,
        force: bool = False,
        dry_run: bool = False,
    ) -> BuffettProcessingResult:
        """
        Check for new 13F filing and process if found.

        Args:
            force: Process even if filing was already processed
            dry_run: Don't send approval request, just calculate

        Returns:
            BuffettProcessingResult with details
        """
        log = logger.bind(force=force, dry_run=dry_run)
        log.info("buffett_check_started")

        # Check for new filing
        detection = await self.detector.check_for_new_filing()

        if detection.error:
            log.error("filing_detection_failed", error=detection.error)
            return BuffettProcessingResult(
                success=False,
                error=detection.error,
            )

        if not detection.new_filing_detected and not force:
            log.info(
                "no_new_filing_to_process",
                current_accession=detection.current_accession,
            )
            return BuffettProcessingResult(
                success=True,
                new_filing=False,
                accession_number=detection.current_accession,
            )

        filing = detection.filing
        if not filing:
            log.error("no_filing_data")
            return BuffettProcessingResult(
                success=False,
                error="No filing data available",
            )

        log = log.bind(
            accession_number=filing.accession_number,
            report_date=str(filing.report_date),
            position_count=filing.position_count,
        )
        log.info("processing_new_filing")

        # Process the filing
        try:
            result = await self._process_filing(filing, dry_run)

            # Mark as processed if successful and not dry run
            if result.success and not dry_run:
                await self.detector.mark_as_processed(
                    accession_number=filing.accession_number,
                    report_date=filing.report_date,
                    details={
                        "trade_count": result.trade_count,
                        "total_buy": result.total_buy,
                        "total_sell": result.total_sell,
                    },
                )

            return result

        except Exception as e:
            log.exception("processing_failed", error=str(e))
            return BuffettProcessingResult(
                success=False,
                new_filing=True,
                accession_number=filing.accession_number,
                error=str(e),
            )

    async def _process_filing(
        self,
        filing,
        dry_run: bool,
    ) -> BuffettProcessingResult:
        """Process a 13F filing and calculate trades."""
        log = logger.bind(accession_number=filing.accession_number)

        # Fetch portfolio via adapter (uses the filing we just got)
        portfolio = await self.adapter.fetch_portfolio()

        if hasattr(portfolio, "error_type"):
            return BuffettProcessingResult(
                success=False,
                new_filing=True,
                accession_number=filing.accession_number,
                error=portfolio.message,
            )

        log.info(
            "portfolio_fetched",
            positions=portfolio.total_positions,
            allocations=len(portfolio.allocations),
        )

        # Load current holdings
        if not HOLDINGS_PATH.exists():
            return BuffettProcessingResult(
                success=False,
                new_filing=True,
                accession_number=filing.accession_number,
                error="No Robinhood holdings found. Run 'make holdings' first.",
            )

        with open(HOLDINGS_PATH) as f:
            holdings_data = json.load(f)

        # Build holdings lookup
        current_holdings = {}
        for h in holdings_data.get("holdings", []):
            symbol = h.get("symbol")
            if symbol:
                current_holdings[symbol] = {
                    "quantity": float(h.get("quantity", 0)),
                    "market_value": float(h.get("market_value", 0)),
                    "price": float(h.get("current_price", 0)),
                }

        # Calculate reconciliation
        dollars_per_weight = self.adapter.config.get("dollars_per_weight", 500)
        deltas = []
        proposed_trades = []

        for alloc in portfolio.allocations:
            target_value = (alloc.raw_weight or 0) * dollars_per_weight
            current = current_holdings.get(alloc.symbol, {})
            current_value = current.get("market_value", 0)
            current_price = current.get("price", 0)
            delta = target_value - current_value

            if abs(delta) < 50:  # $50 threshold
                action = "hold"
            elif delta > 0:
                action = "enter" if current_value == 0 else "buy"
            else:
                action = "sell"

            delta_record = {
                "symbol": alloc.symbol,
                "target_value": target_value,
                "current_value": current_value,
                "suggested_trade_value": delta,
                "action": action,
                "current_price": current_price,
                "notes": f"Buffett sleeve: {alloc.asset_name or ''}",
            }
            deltas.append(delta_record)

            # Create proposed trade if action needed
            if action in ("enter", "buy"):
                proposed_trades.append(
                    ProposedTrade(
                        symbol=alloc.symbol,
                        side="buy",
                        notional=delta,
                        rationale=f"13F target: ${target_value:,.0f}",
                    )
                )
            elif action == "sell":
                proposed_trades.append(
                    ProposedTrade(
                        symbol=alloc.symbol,
                        side="sell",
                        notional=abs(delta),
                        rationale=f"13F target: ${target_value:,.0f}",
                    )
                )

        total_buy = sum(d["suggested_trade_value"] for d in deltas if d["suggested_trade_value"] > 0)
        total_sell = sum(abs(d["suggested_trade_value"]) for d in deltas if d["suggested_trade_value"] < 0)

        log.info(
            "reconciliation_calculated",
            trade_count=len(proposed_trades),
            total_buy=total_buy,
            total_sell=total_sell,
        )

        # Save reconciliation to disk
        recon_output = {
            "sleeve": "buffett",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "accession_number": filing.accession_number,
            "report_date": str(filing.report_date),
            "summary": {
                "total_buy": total_buy,
                "total_sell": total_sell,
                "net_cash_flow": total_sell - total_buy,
                "position_count": len(deltas),
            },
            "deltas": deltas,
        }

        with open(RECONCILIATION_PATH, "w") as f:
            json.dump(recon_output, f, indent=2)

        log.info("reconciliation_saved", path=str(RECONCILIATION_PATH))

        # Send approval request if not dry run and there are trades
        approval_sent = False
        if not dry_run and proposed_trades:
            approval_sent = await self._send_approval_request(
                filing=filing,
                proposed_trades=proposed_trades,
                total_buy=total_buy,
                total_sell=total_sell,
            )

        return BuffettProcessingResult(
            success=True,
            new_filing=True,
            accession_number=filing.accession_number,
            report_date=filing.report_date,
            trade_count=len(proposed_trades),
            total_buy=total_buy,
            total_sell=total_sell,
            approval_sent=approval_sent,
        )

    async def _send_approval_request(
        self,
        filing,
        proposed_trades: list[ProposedTrade],
        total_buy: float,
        total_sell: float,
    ) -> bool:
        """Send approval request to Telegram."""
        from src.approval.workflow import ApprovalWorkflow, get_approval_workflow
        from src.signals.models import ReconciliationPlan, ReconciliationResult
        from uuid import uuid4

        log = logger.bind(
            accession_number=filing.accession_number,
            trade_count=len(proposed_trades),
        )

        try:
            # Get sleeve_id from database
            sleeve_id = None
            try:
                async with get_db_context() as db:
                    sleeve = await sleeve_repository.get_by_name(db, "buffett")
                    if sleeve:
                        sleeve_id = sleeve.id
            except Exception as e:
                log.warning("failed_to_get_sleeve_from_db", error=str(e))

            if not sleeve_id:
                # Fallback to generated UUID
                sleeve_id = uuid4()
                log.warning("using_generated_sleeve_id")

            intent_id = uuid4()

            plan = ReconciliationPlan.create(
                intent_id=intent_id,
                sleeve_id=sleeve_id,
                holdings_snapshot={},  # Not needed for approval display
                proposed_trades=proposed_trades,
                result_type=ReconciliationResult.PROPOSED,
            )

            # Get workflow and send approval
            workflow = get_approval_workflow()
            result = await workflow.process_reconciliation(
                plan=plan,
                sleeve_name="buffett",
                approval_required=True,
            )

            if result.success:
                log.info(
                    "approval_request_sent",
                    approval_id=str(result.approval_id),
                    approval_code=result.approval_code,
                )
                return True
            else:
                log.error("approval_request_failed", error=result.error)
                return False

        except Exception as e:
            log.exception("approval_request_error", error=str(e))
            return False


# Singleton instance
_processor: BuffettSignalProcessor | None = None


def get_buffett_processor() -> BuffettSignalProcessor:
    """Get the Buffett signal processor singleton."""
    global _processor
    if _processor is None:
        _processor = BuffettSignalProcessor()
    return _processor


async def check_and_process_buffett(
    force: bool = False,
    dry_run: bool = False,
) -> BuffettProcessingResult:
    """
    Convenience function to check for and process new Buffett filings.

    Args:
        force: Process even if filing was already processed
        dry_run: Don't send approval request, just calculate

    Returns:
        BuffettProcessingResult with details
    """
    processor = get_buffett_processor()
    return await processor.check_and_process(force=force, dry_run=dry_run)
