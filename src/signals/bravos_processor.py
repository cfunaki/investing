"""
Bravos sleeve signal processor.

Processes new Bravos portfolio update emails using DELTA-ONLY reconciliation:
1. Detects new email via Gmail
2. Scrapes current Bravos active trades
3. Compares against virtual ledger (sleeve_positions)
4. Generates trades ONLY for changed positions
5. Sends delta trades to Telegram for approval

Key principle: Only trade symbols that have changed weights.
Don't touch other positions in the sleeve.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog

from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.repositories.sleeve_position_repository import sleeve_position_repository
from src.db.session import get_db_context
from src.reconciliation.delta_reconciler import (
    DeltaReconciler,
    DeltaTrade,
    get_delta_reconciler,
    parse_bravos_weights,
)
from src.signals.bravos_detector import BravosEmailDetector, get_bravos_detector
from src.signals.models import (
    ProposedTrade,
    ReconciliationPlan,
    ReconciliationResult,
)

logger = structlog.get_logger(__name__)

# Paths
BRAVOS_TRADES_PATH = Path("data/processed/bravos_trades.json")
RECONCILIATION_PATH = Path("data/processed/reconciliation.json")
PROPOSED_ORDERS_PATH = Path("data/processed/proposed_orders.json")


@dataclass
class BravosProcessingResult:
    """Result from processing a Bravos signal."""

    success: bool
    new_email: bool = False
    message_id: str | None = None
    subject: str | None = None
    trade_count: int = 0
    total_buy: float = 0.0
    total_sell: float = 0.0
    approval_sent: bool = False
    error: str | None = None


class BravosSignalProcessor:
    """
    Processes Bravos sleeve signals through the approval workflow.
    """

    def __init__(
        self,
        detector: BravosEmailDetector | None = None,
    ):
        self.detector = detector or get_bravos_detector()

    async def check_and_process(
        self,
        force: bool = False,
        dry_run: bool = False,
        skip_scrape: bool = False,
    ) -> BravosProcessingResult:
        """
        Check for new Bravos email and process if found.

        Args:
            force: Process even if email was already processed
            dry_run: Don't send approval request, just calculate
            skip_scrape: Skip the scraping step (use existing data)

        Returns:
            BravosProcessingResult with details
        """
        log = logger.bind(force=force, dry_run=dry_run, skip_scrape=skip_scrape)
        log.info("bravos_check_started")

        # Check for new email (unless forcing or skipping scrape)
        if not force and not skip_scrape:
            detection = await self.detector.check_for_new_email()

            if detection.error:
                log.error("email_detection_failed", error=detection.error)
                return BravosProcessingResult(
                    success=False,
                    error=detection.error,
                )

            if not detection.new_email_detected:
                log.info("no_new_email_to_process")
                return BravosProcessingResult(
                    success=True,
                    new_email=False,
                )

            email = detection.email
            log = log.bind(
                message_id=email.message_id,
                subject=email.subject,
            )
            log.info("processing_new_email")
        else:
            email = None
            log.info("processing_forced_or_skip_scrape")

        # Process the email/trigger reconciliation
        try:
            result = await self._process_reconciliation(
                email=email,
                dry_run=dry_run,
                skip_scrape=skip_scrape,
            )

            # Mark as processed if successful and not dry run
            if result.success and not dry_run and email:
                await self.detector.mark_as_processed(
                    message_id=email.message_id,
                    subject=email.subject,
                    details={
                        "trade_count": result.trade_count,
                        "total_buy": result.total_buy,
                        "total_sell": result.total_sell,
                    },
                )

            return result

        except Exception as e:
            log.exception("processing_failed", error=str(e))
            return BravosProcessingResult(
                success=False,
                new_email=True if email else False,
                message_id=email.message_id if email else None,
                error=str(e),
            )

    async def _process_reconciliation(
        self,
        email,
        dry_run: bool,
        skip_scrape: bool,
    ) -> BravosProcessingResult:
        """
        Run DELTA-ONLY reconciliation.

        Instead of full portfolio reconciliation, this:
        1. Scrapes current Bravos active trades
        2. Compares weights against virtual ledger
        3. Generates trades ONLY for changed symbols
        """
        log = logger.bind()

        # Step 1: Run scrape if needed (just the Bravos active trades)
        if not skip_scrape:
            log.info("running_bravos_scrape")
            try:
                result = subprocess.run(
                    ["npx", "tsx", "scripts/scrape-active-trades.ts"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                if result.returncode != 0:
                    log.error(
                        "bravos_scrape_failed",
                        returncode=result.returncode,
                        stderr=result.stderr[:500] if result.stderr else None,
                    )
                    return BravosProcessingResult(
                        success=False,
                        new_email=True if email else False,
                        message_id=email.message_id if email else None,
                        error=f"Bravos scrape failed: {result.stderr[:200] if result.stderr else 'Unknown error'}",
                    )

                log.info("bravos_scrape_completed")

            except subprocess.TimeoutExpired:
                log.error("bravos_scrape_timeout")
                return BravosProcessingResult(
                    success=False,
                    new_email=True if email else False,
                    message_id=email.message_id if email else None,
                    error="Bravos scrape timed out",
                )

        # Step 2: Load current Bravos weights
        if not BRAVOS_TRADES_PATH.exists():
            return BravosProcessingResult(
                success=False,
                error="No bravos_trades.json found. Run scrape first.",
            )

        with open(BRAVOS_TRADES_PATH) as f:
            bravos_data = json.load(f)

        new_weights = parse_bravos_weights(bravos_data)
        log.info("bravos_weights_parsed", symbol_count=len(new_weights))

        # Step 3: Get sleeve info
        sleeve_id = None
        try:
            async with get_db_context() as db:
                sleeve = await sleeve_repository.get_by_name(db, "bravos")
                if sleeve:
                    sleeve_id = sleeve.id
        except Exception as e:
            log.warning("failed_to_get_sleeve_from_db", error=str(e))

        if not sleeve_id:
            sleeve_id = uuid4()
            log.warning("using_generated_sleeve_id")

        # Step 4: Run delta reconciliation
        reconciler = get_delta_reconciler()
        delta_result = await reconciler.reconcile(
            sleeve_id=sleeve_id,
            new_weights=new_weights,
        )

        if not delta_result.success:
            return BravosProcessingResult(
                success=False,
                new_email=True if email else False,
                message_id=email.message_id if email else None,
                error=delta_result.error,
            )

        # Convert delta trades to ProposedTrade format
        proposed_trades = [
            ProposedTrade(
                symbol=t.symbol,
                side=t.side,
                notional=float(t.notional),
                rationale=t.rationale,
            )
            for t in delta_result.trades
        ]

        total_buy = float(delta_result.total_buy)
        total_sell = float(delta_result.total_sell)

        log.info(
            "delta_reconciliation_complete",
            trade_count=len(proposed_trades),
            total_buy=total_buy,
            total_sell=total_sell,
            changes=[
                {"symbol": c.symbol, "action": c.action, "delta": float(c.weight_delta)}
                for c in delta_result.weight_changes
            ],
        )

        # Send approval request if not dry run and there are trades
        approval_sent = False
        if not dry_run and proposed_trades:
            approval_sent = await self._send_approval_request(
                email=email,
                proposed_trades=proposed_trades,
                total_buy=total_buy,
                total_sell=total_sell,
            )

        return BravosProcessingResult(
            success=True,
            new_email=True if email else False,
            message_id=email.message_id if email else None,
            subject=email.subject if email else None,
            trade_count=len(proposed_trades),
            total_buy=total_buy,
            total_sell=total_sell,
            approval_sent=approval_sent,
        )

    async def _send_approval_request(
        self,
        email,
        proposed_trades: list[ProposedTrade],
        total_buy: float,
        total_sell: float,
    ) -> bool:
        """Send approval request to Telegram."""
        from src.approval.workflow import get_approval_workflow

        log = logger.bind(
            message_id=email.message_id if email else None,
            trade_count=len(proposed_trades),
        )

        try:
            # Get sleeve_id from database
            sleeve_id = None
            try:
                async with get_db_context() as db:
                    sleeve = await sleeve_repository.get_by_name(db, "bravos")
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
                sleeve_name="bravos",
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
_processor: BravosSignalProcessor | None = None


def get_bravos_processor() -> BravosSignalProcessor:
    """Get the Bravos signal processor singleton."""
    global _processor
    if _processor is None:
        _processor = BravosSignalProcessor()
    return _processor


async def check_and_process_bravos(
    force: bool = False,
    dry_run: bool = False,
    skip_scrape: bool = False,
) -> BravosProcessingResult:
    """
    Convenience function to check for and process new Bravos emails.

    Args:
        force: Process even if email was already processed
        dry_run: Don't send approval request, just calculate
        skip_scrape: Skip scraping, use existing data

    Returns:
        BravosProcessingResult with details
    """
    processor = get_bravos_processor()
    return await processor.check_and_process(
        force=force,
        dry_run=dry_run,
        skip_scrape=skip_scrape,
    )
