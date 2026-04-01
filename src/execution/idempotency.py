"""
Order-level idempotency tracking.

This module ensures we never accidentally place duplicate orders.
This is CRITICAL for trade execution safety.

The idempotency key is generated from:
- Approval ID (unique per reconciliation approval)
- Symbol
- Side (buy/sell)

If a broker call times out or fails, we check for existing orders
before retrying to prevent duplicate executions.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import structlog

from src.brokers.base import OrderResult, OrderSide, OrderStatus
from src.db.repositories.execution_repository import execution_repository
from src.db.session import get_db_context

logger = structlog.get_logger(__name__)


class ExecutionState(str, Enum):
    """State of an execution attempt."""

    PENDING = "pending"  # Not yet attempted
    IN_PROGRESS = "in_progress"  # Currently executing
    SUBMITTED = "submitted"  # Order submitted to broker
    FILLED = "filled"  # Order completely filled
    PARTIAL = "partial"  # Order partially filled
    FAILED = "failed"  # Order failed
    CANCELLED = "cancelled"  # Order cancelled


@dataclass
class ExecutionRecord:
    """
    Record of an execution attempt.

    Tracks the state and details of order execution
    for idempotency and audit purposes.
    """

    execution_key: str
    approval_id: UUID
    symbol: str
    side: OrderSide
    notional: float

    state: ExecutionState = ExecutionState.PENDING
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    filled_price: float | None = None
    filled_notional: float = 0.0
    error: str | None = None
    broker_response: dict[str, Any] | None = None

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attempts: int = 0


def generate_execution_key(
    approval_id: UUID,
    symbol: str,
    side: OrderSide,
) -> str:
    """
    Generate a unique execution key.

    The key is a hash of the approval ID, symbol, and side.
    This ensures that for a given approval, each symbol/side
    combination can only be executed once.

    Args:
        approval_id: The approval ID
        symbol: The trading symbol
        side: Buy or sell

    Returns:
        Unique execution key (hex string)
    """
    key_input = f"{approval_id}:{symbol.upper()}:{side.value}"
    return hashlib.sha256(key_input.encode()).hexdigest()[:16]


class IdempotencyTracker:
    """
    Tracks execution attempts for idempotency.

    This is the critical component that prevents duplicate orders.
    Uses database for durability - executions survive restarts.
    Also maintains in-memory cache for fast lookups during execution.
    """

    def __init__(self):
        """Initialize the tracker."""
        # In-memory cache for current session
        self._executions: dict[str, ExecutionRecord] = {}

    async def get_or_create(
        self,
        approval_id: UUID,
        symbol: str,
        side: OrderSide,
        notional: float,
    ) -> tuple[ExecutionRecord, bool]:
        """
        Get existing execution record or create a new one.

        Args:
            approval_id: The approval ID
            symbol: The trading symbol
            side: Buy or sell
            notional: Expected notional amount

        Returns:
            Tuple of (record, is_new) where is_new is True if created
        """
        key = generate_execution_key(approval_id, symbol, side)

        # Check in-memory cache first
        if key in self._executions:
            existing = self._executions[key]
            logger.info(
                "found_existing_execution_in_cache",
                execution_key=key,
                state=existing.state.value,
                broker_order_id=existing.broker_order_id,
            )
            return existing, False

        # Check database
        try:
            async with get_db_context() as db:
                db_execution = await execution_repository.get_by_key(db, key)
                if db_execution:
                    # Found in database - recreate ExecutionRecord
                    state_map = {
                        "pending": ExecutionState.PENDING,
                        "submitted": ExecutionState.SUBMITTED,
                        "filled": ExecutionState.FILLED,
                        "partial": ExecutionState.PARTIAL,
                        "failed": ExecutionState.FAILED,
                        "cancelled": ExecutionState.CANCELLED,
                    }
                    record = ExecutionRecord(
                        execution_key=key,
                        approval_id=db_execution.approval_id,
                        symbol=db_execution.symbol,
                        side=OrderSide(db_execution.side),
                        notional=float(db_execution.notional or 0),
                        state=state_map.get(db_execution.status, ExecutionState.PENDING),
                        broker_order_id=db_execution.broker_order_id,
                        broker_response=db_execution.broker_response,
                    )
                    self._executions[key] = record
                    logger.info(
                        "found_existing_execution_in_database",
                        execution_key=key,
                        state=record.state.value,
                        broker_order_id=record.broker_order_id,
                    )
                    return record, False

                # Not found - create new record in database
                db_execution = await execution_repository.create(
                    db=db,
                    approval_id=approval_id,
                    symbol=symbol,
                    side=side.value,
                    execution_key=key,
                    notional=Decimal(str(notional)),
                    status="pending",
                )
                logger.info(
                    "created_execution_in_database",
                    execution_key=key,
                    execution_id=str(db_execution.id),
                )

        except Exception as e:
            logger.warning("database_operation_failed", error=str(e))
            # Continue with in-memory only

        # Create in-memory record
        record = ExecutionRecord(
            execution_key=key,
            approval_id=approval_id,
            symbol=symbol,
            side=side,
            notional=notional,
        )

        self._executions[key] = record
        logger.info(
            "created_execution_record",
            execution_key=key,
            symbol=symbol,
            side=side.value,
        )

        return record, True

    def mark_in_progress(self, key: str) -> bool:
        """
        Mark an execution as in progress.

        Returns:
            True if successfully marked, False if already in progress
        """
        record = self._executions.get(key)
        if not record:
            return False

        if record.state == ExecutionState.IN_PROGRESS:
            logger.warning("execution_already_in_progress", execution_key=key)
            return False

        if record.state in (ExecutionState.FILLED, ExecutionState.SUBMITTED):
            logger.warning(
                "cannot_mark_in_progress_already_executed",
                execution_key=key,
                state=record.state.value,
            )
            return False

        record.state = ExecutionState.IN_PROGRESS
        record.attempts += 1
        record.updated_at = datetime.now(timezone.utc)

        logger.info(
            "execution_marked_in_progress",
            execution_key=key,
            attempt=record.attempts,
        )

        return True

    async def update_from_order_result(
        self,
        key: str,
        result: OrderResult,
    ) -> ExecutionRecord | None:
        """
        Update execution record from broker order result.

        Args:
            key: The execution key
            result: Order result from broker

        Returns:
            Updated record or None if not found
        """
        record = self._executions.get(key)
        if not record:
            logger.warning("execution_record_not_found", execution_key=key)
            return None

        # Map order status to execution state
        status_map = {
            OrderStatus.SUBMITTED: ExecutionState.SUBMITTED,
            OrderStatus.FILLED: ExecutionState.FILLED,
            OrderStatus.PARTIAL: ExecutionState.PARTIAL,
            OrderStatus.CANCELLED: ExecutionState.CANCELLED,
            OrderStatus.REJECTED: ExecutionState.FAILED,
            OrderStatus.FAILED: ExecutionState.FAILED,
        }

        # Map to database status strings
        db_status_map = {
            OrderStatus.SUBMITTED: "submitted",
            OrderStatus.FILLED: "filled",
            OrderStatus.PARTIAL: "partial",
            OrderStatus.CANCELLED: "cancelled",
            OrderStatus.REJECTED: "failed",
            OrderStatus.FAILED: "failed",
        }

        record.state = status_map.get(result.status, ExecutionState.PENDING)
        record.broker_order_id = result.order_id
        record.filled_quantity = result.filled_quantity
        record.filled_price = result.filled_price
        record.filled_notional = result.filled_notional
        record.error = result.error
        record.broker_response = result.broker_response
        record.updated_at = datetime.now(timezone.utc)

        # Update database
        try:
            async with get_db_context() as db:
                db_execution = await execution_repository.get_by_key(db, key)
                if db_execution:
                    await execution_repository.update_status(
                        db=db,
                        execution_id=db_execution.id,
                        status=db_status_map.get(result.status, "pending"),
                        broker_order_id=result.order_id,
                        broker_response=result.broker_response,
                        executed_at=record.updated_at,
                    )
                    logger.info("execution_updated_in_database", execution_key=key)
        except Exception as e:
            logger.warning("failed_to_update_execution_in_db", error=str(e))

        logger.info(
            "execution_record_updated",
            execution_key=key,
            state=record.state.value,
            broker_order_id=record.broker_order_id,
            filled_quantity=record.filled_quantity,
        )

        return record

    async def mark_failed(self, key: str, error: str) -> ExecutionRecord | None:
        """Mark an execution as failed."""
        record = self._executions.get(key)
        if not record:
            return None

        record.state = ExecutionState.FAILED
        record.error = error
        record.updated_at = datetime.now(timezone.utc)

        # Update database
        try:
            async with get_db_context() as db:
                db_execution = await execution_repository.get_by_key(db, key)
                if db_execution:
                    await execution_repository.update_status(
                        db=db,
                        execution_id=db_execution.id,
                        status="failed",
                        broker_response={"error": error},
                    )
        except Exception as e:
            logger.warning("failed_to_update_failed_execution_in_db", error=str(e))

        logger.warning(
            "execution_marked_failed",
            execution_key=key,
            error=error,
        )

        return record

    async def is_safe_to_execute(self, key: str) -> tuple[bool, str]:
        """
        Check if it's safe to execute an order.

        This is the critical idempotency check. Returns False if:
        - Order is already submitted/filled
        - Order is currently in progress

        Args:
            key: The execution key

        Returns:
            Tuple of (is_safe, reason)
        """
        # Check in-memory cache first
        record = self._executions.get(key)

        # If not in cache, check database
        if not record:
            try:
                async with get_db_context() as db:
                    db_execution = await execution_repository.get_by_key(db, key)
                    if db_execution:
                        # Execution exists in database - check status
                        status = db_execution.status
                        if status == "filled":
                            return False, f"already_filled:order_id={db_execution.broker_order_id}"
                        if status == "submitted":
                            return False, f"already_submitted:order_id={db_execution.broker_order_id}"
                        if status == "partial":
                            return True, "partial_fill:can_retry"
                        if status == "failed":
                            return True, "previous_attempt_failed:can_retry"
                        return True, f"db_status={status}"
            except Exception as e:
                logger.warning("failed_to_check_db_for_execution", error=str(e))
            return True, "no_existing_record"

        if record.state == ExecutionState.FILLED:
            return False, f"already_filled:order_id={record.broker_order_id}"

        if record.state == ExecutionState.SUBMITTED:
            return False, f"already_submitted:order_id={record.broker_order_id}"

        if record.state == ExecutionState.IN_PROGRESS:
            return False, "execution_in_progress"

        if record.state == ExecutionState.PARTIAL:
            # Partial fill - might be OK to retry for remainder
            return True, f"partial_fill:filled={record.filled_quantity}"

        if record.state == ExecutionState.FAILED:
            # Failed - OK to retry
            return True, f"previous_attempt_failed:error={record.error}"

        return True, f"state={record.state.value}"

    def get_execution(self, key: str) -> ExecutionRecord | None:
        """Get an execution record by key."""
        return self._executions.get(key)

    def get_executions_for_approval(self, approval_id: UUID) -> list[ExecutionRecord]:
        """Get all execution records for an approval."""
        return [
            record
            for record in self._executions.values()
            if record.approval_id == approval_id
        ]

    def get_pending_executions(self) -> list[ExecutionRecord]:
        """Get all pending execution records."""
        return [
            record
            for record in self._executions.values()
            if record.state in (ExecutionState.PENDING, ExecutionState.FAILED)
        ]


# Singleton instance
_tracker: IdempotencyTracker | None = None


def get_idempotency_tracker() -> IdempotencyTracker:
    """Get the idempotency tracker singleton."""
    global _tracker
    if _tracker is None:
        _tracker = IdempotencyTracker()
    return _tracker


async def check_execution_safety(
    approval_id: UUID,
    symbol: str,
    side: OrderSide,
) -> tuple[bool, str, str]:
    """
    Convenience function to check if execution is safe.

    Args:
        approval_id: The approval ID
        symbol: The trading symbol
        side: Buy or sell

    Returns:
        Tuple of (is_safe, reason, execution_key)
    """
    tracker = get_idempotency_tracker()
    key = generate_execution_key(approval_id, symbol, side)
    is_safe, reason = await tracker.is_safe_to_execute(key)
    return is_safe, reason, key
