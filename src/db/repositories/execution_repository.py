"""
Repository for Execution database operations.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Execution


class ExecutionRepository:
    """Data access layer for trade executions."""

    async def create(
        self,
        db: AsyncSession,
        approval_id: UUID,
        symbol: str,
        side: str,
        execution_key: str,
        quantity: Decimal | None = None,
        notional: Decimal | None = None,
        status: str = "pending",
    ) -> Execution:
        """Create a new execution record."""
        execution = Execution(
            approval_id=approval_id,
            symbol=symbol,
            side=side,
            execution_key=execution_key,
            quantity=quantity,
            notional=notional,
            status=status,
        )
        db.add(execution)
        await db.flush()
        return execution

    async def get_by_key(self, db: AsyncSession, execution_key: str) -> Execution | None:
        """Get an execution by its idempotency key."""
        stmt = select(Execution).where(Execution.execution_key == execution_key)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, db: AsyncSession, execution_id: UUID) -> Execution | None:
        """Get an execution by its ID."""
        stmt = select(Execution).where(Execution.id == execution_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_approval(self, db: AsyncSession, approval_id: UUID) -> list[Execution]:
        """Get all executions for an approval."""
        stmt = (
            select(Execution)
            .where(Execution.approval_id == approval_id)
            .order_by(Execution.created_at)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        db: AsyncSession,
        execution_id: UUID,
        status: str,
        broker_order_id: str | None = None,
        broker_response: dict | None = None,
        executed_at: datetime | None = None,
    ) -> Execution | None:
        """Update execution status after broker response."""
        values = {"status": status}
        if broker_order_id is not None:
            values["broker_order_id"] = broker_order_id
        if broker_response is not None:
            values["broker_response"] = broker_response
        if executed_at is not None:
            values["executed_at"] = executed_at

        stmt = (
            update(Execution)
            .where(Execution.id == execution_id)
            .values(**values)
            .returning(Execution)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_recent(self, db: AsyncSession, limit: int = 20) -> list[Execution]:
        """Get recent executions."""
        stmt = (
            select(Execution)
            .order_by(Execution.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


# Singleton instance
execution_repository = ExecutionRepository()
