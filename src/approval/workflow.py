"""
Approval workflow state machine.

This module orchestrates the approval process:
1. Receives reconciliation results
2. Creates approval requests
3. Sends to Telegram
4. Handles responses (approve/reject)
5. Triggers execution on approval
6. Handles manual review cases

The workflow is the bridge between signal processing and trade execution.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Coroutine
from uuid import UUID, uuid4

import structlog

from src.approval.telegram import ApprovalRequest, TelegramBot, generate_approval_code, get_telegram_bot
from src.config import get_settings
from src.db.repositories.approval_repository import approval_repository
from src.db.repositories.reconciliation_repository import reconciliation_repository
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context
from src.execution.executor import execute_approved_trades
from src.signals.models import (
    PortfolioIntent,
    ProposedTrade,
    ReconciliationPlan,
    ReconciliationResult,
    ReviewStatus,
)

logger = structlog.get_logger(__name__)


class ApprovalStatus(str, Enum):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class ApprovalRecord:
    """
    Record of an approval request.

    In production, this would be stored in the database.
    For now, we track state in memory.
    """

    id: UUID
    reconciliation_id: UUID
    sleeve_id: UUID
    approval_code: str
    proposed_trades: list[dict[str, Any]]
    total_notional: float

    status: ApprovalStatus = ApprovalStatus.PENDING
    telegram_message_id: int | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    responded_at: datetime | None = None
    approved_by: str | None = None

    def is_expired(self) -> bool:
        """Check if the approval has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at


@dataclass
class WorkflowResult:
    """Result from a workflow operation."""

    success: bool
    approval_id: UUID | None = None
    approval_code: str | None = None
    status: str | None = None
    error: str | None = None
    details: dict[str, Any] | None = None


class ApprovalWorkflow:
    """
    Manages the approval workflow for trade execution.

    This is the central coordinator that:
    1. Receives reconciliation plans
    2. Decides if approval is needed (based on sleeve config)
    3. Creates and sends approval requests
    4. Tracks approval status
    5. Triggers execution when approved
    6. Handles manual review cases
    """

    def __init__(
        self,
        telegram_bot: TelegramBot | None = None,
        on_approved: Callable[[UUID, list[dict]], Coroutine] | None = None,
        on_rejected: Callable[[UUID], Coroutine] | None = None,
    ):
        """
        Initialize the workflow.

        Args:
            telegram_bot: Telegram bot for sending messages
            on_approved: Callback when approval is granted (receives approval_id, trades)
            on_rejected: Callback when approval is rejected
        """
        self.bot = telegram_bot or get_telegram_bot()
        self.on_approved = on_approved
        self.on_rejected = on_rejected

        # In-memory approval store (in production, use database)
        self._approvals: dict[str, ApprovalRecord] = {}  # Keyed by approval_code
        self._approvals_by_id: dict[UUID, ApprovalRecord] = {}

        # Wire up bot callbacks
        self.bot.on_approval = self._handle_approval
        self.bot.on_rejection = self._handle_rejection
        self.bot.on_retry = self._handle_retry

    async def process_reconciliation(
        self,
        plan: ReconciliationPlan,
        sleeve_name: str,
        approval_required: bool = True,
    ) -> WorkflowResult:
        """
        Process a reconciliation plan through the approval workflow.

        Args:
            plan: The reconciliation plan with proposed trades
            sleeve_name: Name of the sleeve
            approval_required: Whether this sleeve requires approval

        Returns:
            WorkflowResult indicating what happened
        """
        log = logger.bind(
            reconciliation_id=str(plan.id),
            sleeve=sleeve_name,
            trade_count=plan.trade_count,
        )

        log.info("processing_reconciliation_for_approval")

        # If no trades, nothing to approve
        if not plan.has_trades:
            log.info("no_trades_to_approve")
            return WorkflowResult(
                success=True,
                status="no_action",
                details={"reason": "No trades proposed"},
            )

        # If manual review required, send alert instead of approval request
        if plan.result_type == ReconciliationResult.MANUAL_REVIEW:
            return await self._send_review_alert(plan, sleeve_name)

        # If approval not required, auto-approve
        if not approval_required:
            log.info("auto_approving_trades")
            if self.on_approved:
                await self.on_approved(plan.id, plan.trades_to_dict())
            return WorkflowResult(
                success=True,
                status="auto_approved",
                details={"trades": plan.trade_count},
            )

        # Create and send approval request
        return await self._create_approval_request(plan, sleeve_name)

    async def _create_approval_request(
        self,
        plan: ReconciliationPlan,
        sleeve_name: str,
    ) -> WorkflowResult:
        """Create and send an approval request."""
        settings = get_settings()

        approval_code = generate_approval_code()
        requested_at = datetime.now(timezone.utc)
        expires_at = requested_at + timedelta(minutes=settings.approval_expiry_minutes)

        log = logger.bind(
            approval_code=approval_code,
            trades=plan.trade_count,
            notional=plan.total_notional,
            sleeve=sleeve_name,
        )

        # Persist to database: Signal → Intent → Reconciliation → Approval
        try:
            async with get_db_context() as db:
                # Get sleeve from database
                sleeve = await sleeve_repository.get_by_name(db, sleeve_name)
                if not sleeve:
                    log.warning("sleeve_not_found_using_plan_id", sleeve_name=sleeve_name)
                    sleeve_id = plan.sleeve_id
                else:
                    sleeve_id = sleeve.id

                # Create the full chain: Signal → Intent → Reconciliation
                source_event_id = f"approval_{approval_code}_{requested_at.timestamp()}"
                signal, intent, recon = await reconciliation_repository.create_full_chain(
                    db=db,
                    sleeve_id=sleeve_id,
                    source_event_id=source_event_id,
                    event_type="approval_request",
                    proposed_trades=plan.trades_to_dict(),
                    holdings_snapshot=plan.holdings_snapshot if hasattr(plan, 'holdings_snapshot') else {},
                    target_allocations=plan.trades_to_dict(),
                )

                # Create approval record in database
                db_approval = await approval_repository.create(
                    db=db,
                    reconciliation_id=recon.id,
                    approval_code=approval_code,
                    proposed_trades=plan.trades_to_dict(),
                    requested_at=requested_at,
                    expires_at=expires_at,
                )
                approval_id = db_approval.id

                log = log.bind(
                    approval_id=str(approval_id),
                    signal_id=str(signal.id),
                    recon_id=str(recon.id),
                )
                log.info("approval_persisted_to_database")

        except Exception as e:
            log.exception("failed_to_persist_approval", error=str(e))
            # Fall back to in-memory only
            approval_id = uuid4()
            log.warning("using_in_memory_approval_only")

        # Create in-memory record for session tracking
        record = ApprovalRecord(
            id=approval_id,
            reconciliation_id=plan.id,
            sleeve_id=plan.sleeve_id,
            approval_code=approval_code,
            proposed_trades=plan.trades_to_dict(),
            total_notional=plan.total_notional,
            expires_at=expires_at,
        )

        # Create request for Telegram
        request = ApprovalRequest(
            approval_id=record.id,
            reconciliation_id=plan.id,
            sleeve_name=sleeve_name,
            proposed_trades=record.proposed_trades,
            total_notional=record.total_notional,
            approval_code=approval_code,
            expires_at=expires_at,
        )

        # Send to Telegram
        message_id = await self.bot.send_approval_request(request)

        if message_id is None:
            log.error("failed_to_send_approval_request")
            return WorkflowResult(
                success=False,
                error="Failed to send approval request to Telegram",
            )

        # Update Telegram message ID in database
        try:
            async with get_db_context() as db:
                await approval_repository.set_telegram_message_id(
                    db, approval_id, str(message_id)
                )
        except Exception as e:
            log.warning("failed_to_update_telegram_message_id", error=str(e))

        # Store the in-memory record
        record.telegram_message_id = message_id
        self._approvals[approval_code] = record
        self._approvals_by_id[record.id] = record

        log.info("approval_request_sent", message_id=message_id)

        return WorkflowResult(
            success=True,
            approval_id=record.id,
            approval_code=approval_code,
            status="pending",
            details={
                "trades": plan.trade_count,
                "total_notional": plan.total_notional,
                "expires_at": expires_at.isoformat(),
            },
        )

    async def _send_review_alert(
        self,
        plan: ReconciliationPlan,
        sleeve_name: str,
    ) -> WorkflowResult:
        """Send a manual review alert instead of approval request."""
        log = logger.bind(
            reconciliation_id=str(plan.id),
            sleeve=sleeve_name,
            reason=plan.review_reason,
        )

        log.info("sending_review_alert")

        message_id = await self.bot.send_review_alert(
            sleeve_name=sleeve_name,
            reason=plan.review_reason or "Unknown reason",
            intent_id=str(plan.intent_id),
            details={
                "trade_count": plan.trade_count,
                "total_notional": plan.total_notional,
            },
        )

        if message_id is None:
            log.error("failed_to_send_review_alert")
            return WorkflowResult(
                success=False,
                error="Failed to send review alert to Telegram",
            )

        log.info("review_alert_sent", message_id=message_id)

        return WorkflowResult(
            success=True,
            status="manual_review",
            details={
                "reason": plan.review_reason,
                "message_id": message_id,
            },
        )

    async def process_intent_for_review(
        self,
        intent: PortfolioIntent,
        sleeve_name: str,
    ) -> WorkflowResult:
        """
        Send a review alert for an intent that failed validation.

        This is called when the signal processor flags an intent for review
        before reconciliation even happens.
        """
        log = logger.bind(
            intent_id=str(intent.id),
            sleeve=sleeve_name,
            reason=intent.review_reason,
        )

        log.info("sending_intent_review_alert")

        message_id = await self.bot.send_review_alert(
            sleeve_name=sleeve_name,
            reason=intent.review_reason or "Validation failed",
            intent_id=str(intent.id),
            details={
                "positions": intent.position_count,
                "confidence": f"{intent.confidence:.0%}",
                "total_weight": f"{intent.total_weight:.1%}",
            },
        )

        if message_id:
            log.info("intent_review_alert_sent", message_id=message_id)
            return WorkflowResult(
                success=True,
                status="review_alert_sent",
                details={"message_id": message_id},
            )
        else:
            log.error("failed_to_send_intent_review_alert")
            return WorkflowResult(
                success=False,
                error="Failed to send review alert",
            )

    async def _handle_approval(
        self,
        approval_id: UUID,
        approved_by: str,
        user_name: str | None = None,
    ):
        """Handle approval callback from Telegram."""
        log = logger.bind(
            approval_id=str(approval_id),
            approved_by=approved_by,
        )

        record = self._approvals_by_id.get(approval_id)
        if not record:
            # Try to load from database
            try:
                async with get_db_context() as db:
                    db_approval = await approval_repository.get_by_id(db, approval_id)
                    if db_approval:
                        record = ApprovalRecord(
                            id=db_approval.id,
                            reconciliation_id=db_approval.reconciliation_id,
                            sleeve_id=uuid4(),  # Not stored in approval, use placeholder
                            approval_code=db_approval.approval_code,
                            proposed_trades=db_approval.proposed_trades,
                            total_notional=sum(t.get("notional", 0) for t in db_approval.proposed_trades),
                            status=ApprovalStatus(db_approval.status),
                            expires_at=db_approval.expires_at,
                        )
                        self._approvals_by_id[approval_id] = record
                        self._approvals[db_approval.approval_code] = record
                        log.info("loaded_approval_from_database")
            except Exception as e:
                log.warning("failed_to_load_approval_from_db", error=str(e))

        if not record:
            log.warning("approval_record_not_found")
            return

        responded_at = datetime.now(timezone.utc)

        # Update in-memory record
        record.status = ApprovalStatus.APPROVED
        record.responded_at = responded_at
        record.approved_by = approved_by

        # Update database record
        try:
            async with get_db_context() as db:
                await approval_repository.update_status(
                    db=db,
                    approval_id=approval_id,
                    status="approved",
                    approved_by=approved_by,
                    responded_at=responded_at,
                )
                log.info("approval_status_updated_in_database")
        except Exception as e:
            log.warning("failed_to_update_approval_in_db", error=str(e))

        log.info("approval_granted")

        # Execute the approved trades
        try:
            log.info("executing_approved_trades", trade_count=len(record.proposed_trades))

            execution_report = await execute_approved_trades(
                approval_id=record.id,
                trades=record.proposed_trades,
            )

            log.info(
                "execution_completed",
                success=execution_report.success,
                executed=execution_report.executed,
                failed=execution_report.failed,
            )

        except Exception as e:
            log.exception("execution_failed", error=str(e))
            await self.bot.send_notification(
                f"*Execution Failed*\n"
                f"Approval: `{record.approval_code}`\n"
                f"Error: {str(e)}"
            )

        # Trigger additional callback if set
        if self.on_approved:
            await self.on_approved(record.reconciliation_id, record.proposed_trades)

    async def _handle_rejection(
        self,
        approval_id: UUID,
        rejected_by: str,
        user_name: str | None = None,
    ):
        """Handle rejection callback from Telegram."""
        log = logger.bind(
            approval_id=str(approval_id),
            rejected_by=rejected_by,
        )

        record = self._approvals_by_id.get(approval_id)
        if not record:
            # Try to load from database
            try:
                async with get_db_context() as db:
                    db_approval = await approval_repository.get_by_id(db, approval_id)
                    if db_approval:
                        record = ApprovalRecord(
                            id=db_approval.id,
                            reconciliation_id=db_approval.reconciliation_id,
                            sleeve_id=uuid4(),
                            approval_code=db_approval.approval_code,
                            proposed_trades=db_approval.proposed_trades,
                            total_notional=sum(t.get("notional", 0) for t in db_approval.proposed_trades),
                            status=ApprovalStatus(db_approval.status),
                            expires_at=db_approval.expires_at,
                        )
                        self._approvals_by_id[approval_id] = record
                        self._approvals[db_approval.approval_code] = record
            except Exception as e:
                log.warning("failed_to_load_approval_from_db", error=str(e))

        if not record:
            log.warning("approval_record_not_found")
            return

        responded_at = datetime.now(timezone.utc)

        # Update in-memory record
        record.status = ApprovalStatus.REJECTED
        record.responded_at = responded_at
        record.approved_by = rejected_by  # Track who rejected

        # Update database record
        try:
            async with get_db_context() as db:
                await approval_repository.update_status(
                    db=db,
                    approval_id=approval_id,
                    status="rejected",
                    approved_by=rejected_by,
                    responded_at=responded_at,
                )
                log.info("rejection_status_updated_in_database")
        except Exception as e:
            log.warning("failed_to_update_rejection_in_db", error=str(e))

        log.info("approval_rejected")

        # Trigger rejection callback
        if self.on_rejected:
            await self.on_rejected(record.reconciliation_id)

    async def _handle_retry(
        self,
        signal_id: str,
        requested_by: str,
    ):
        """Handle retry request from Telegram command."""
        log = logger.bind(signal_id=signal_id, requested_by=requested_by)
        log.info("retry_requested")

        # TODO: Look up signal and re-process
        # This would call back into the signal processor

        await self.bot.send_notification(
            f"Retry for signal `{signal_id}` acknowledged.\n"
            "Re-processing will be implemented in the next phase.",
        )

    async def expire_pending_approvals(self) -> int:
        """
        Expire any pending approvals that have passed their expiration time.

        Returns:
            Number of approvals expired
        """
        expired_count = 0
        now = datetime.now(timezone.utc)

        # Expire in-memory approvals
        for code, record in list(self._approvals.items()):
            if record.status == ApprovalStatus.PENDING and record.is_expired():
                record.status = ApprovalStatus.EXPIRED
                expired_count += 1

                logger.info(
                    "approval_expired",
                    approval_code=code,
                    approval_id=str(record.id),
                )

                # Update Telegram message
                await self.bot.update_approval_message(
                    code,
                    f"*Approval Expired*\n"
                    f"Code: `{code}`\n"
                    f"Expired at: {now.strftime('%Y-%m-%d %H:%M UTC')}",
                )

        # Expire database approvals
        try:
            async with get_db_context() as db:
                db_expired = await approval_repository.get_expired(db)
                for db_approval in db_expired:
                    await approval_repository.mark_expired(db, db_approval.id)
                    # Only count if not already counted from in-memory
                    if db_approval.approval_code not in self._approvals:
                        expired_count += 1
                        logger.info(
                            "db_approval_expired",
                            approval_code=db_approval.approval_code,
                            approval_id=str(db_approval.id),
                        )
        except Exception as e:
            logger.warning("failed_to_expire_db_approvals", error=str(e))

        return expired_count

    def get_pending_approvals(self) -> list[ApprovalRecord]:
        """Get all pending approval records from in-memory cache."""
        return [
            record
            for record in self._approvals.values()
            if record.status == ApprovalStatus.PENDING
        ]

    async def get_pending_approvals_from_db(self) -> list[ApprovalRecord]:
        """Get all pending approval records from database."""
        records = []
        try:
            async with get_db_context() as db:
                db_approvals = await approval_repository.get_pending(db)
                for db_approval in db_approvals:
                    record = ApprovalRecord(
                        id=db_approval.id,
                        reconciliation_id=db_approval.reconciliation_id,
                        sleeve_id=uuid4(),
                        approval_code=db_approval.approval_code,
                        proposed_trades=db_approval.proposed_trades,
                        total_notional=sum(t.get("notional", 0) for t in db_approval.proposed_trades),
                        status=ApprovalStatus(db_approval.status),
                        telegram_message_id=int(db_approval.telegram_message_id) if db_approval.telegram_message_id else None,
                        requested_at=db_approval.requested_at,
                        expires_at=db_approval.expires_at,
                    )
                    records.append(record)
                    # Also cache in memory
                    self._approvals[db_approval.approval_code] = record
                    self._approvals_by_id[db_approval.id] = record
        except Exception as e:
            logger.warning("failed_to_get_pending_from_db", error=str(e))
        return records

    def get_approval_by_code(self, code: str) -> ApprovalRecord | None:
        """Get an approval record by its code (in-memory only)."""
        return self._approvals.get(code)

    async def get_approval_by_code_async(self, code: str) -> ApprovalRecord | None:
        """Get an approval record by its code, falling back to database."""
        record = self._approvals.get(code)
        if record:
            return record

        try:
            async with get_db_context() as db:
                db_approval = await approval_repository.get_by_code(db, code)
                if db_approval:
                    record = ApprovalRecord(
                        id=db_approval.id,
                        reconciliation_id=db_approval.reconciliation_id,
                        sleeve_id=uuid4(),
                        approval_code=db_approval.approval_code,
                        proposed_trades=db_approval.proposed_trades,
                        total_notional=sum(t.get("notional", 0) for t in db_approval.proposed_trades),
                        status=ApprovalStatus(db_approval.status),
                        expires_at=db_approval.expires_at,
                    )
                    self._approvals[code] = record
                    self._approvals_by_id[db_approval.id] = record
                    return record
        except Exception as e:
            logger.warning("failed_to_get_approval_from_db", error=str(e))
        return None

    def get_approval_by_id(self, approval_id: UUID) -> ApprovalRecord | None:
        """Get an approval record by its ID (in-memory only)."""
        return self._approvals_by_id.get(approval_id)


# Singleton instance
_workflow: ApprovalWorkflow | None = None


def get_approval_workflow() -> ApprovalWorkflow:
    """Get the approval workflow singleton."""
    global _workflow
    if _workflow is None:
        _workflow = ApprovalWorkflow()
    return _workflow


async def process_for_approval(
    plan: ReconciliationPlan,
    sleeve_name: str,
    approval_required: bool = True,
) -> WorkflowResult:
    """
    Convenience function to process a reconciliation plan for approval.

    Args:
        plan: The reconciliation plan
        sleeve_name: Name of the sleeve
        approval_required: Whether approval is required

    Returns:
        WorkflowResult indicating what happened
    """
    workflow = get_approval_workflow()
    return await workflow.process_reconciliation(
        plan=plan,
        sleeve_name=sleeve_name,
        approval_required=approval_required,
    )
