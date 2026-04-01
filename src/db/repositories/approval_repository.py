"""
Repository for Approval database operations.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Approval


class ApprovalRepository:
    """Data access layer for approvals."""

    async def create(
        self,
        db: AsyncSession,
        reconciliation_id: UUID,
        approval_code: str,
        proposed_trades: list[dict],
        requested_at: datetime,
        expires_at: datetime,
        telegram_message_id: str | None = None,
    ) -> Approval:
        """Create a new approval request."""
        approval = Approval(
            reconciliation_id=reconciliation_id,
            approval_code=approval_code,
            proposed_trades=proposed_trades,
            telegram_message_id=telegram_message_id,
            status="pending",
            requested_at=requested_at,
            expires_at=expires_at,
        )
        db.add(approval)
        await db.flush()
        return approval

    async def get_by_code(self, db: AsyncSession, code: str) -> Approval | None:
        """Get an approval by its short code."""
        stmt = select(Approval).where(Approval.approval_code == code)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, db: AsyncSession, approval_id: UUID) -> Approval | None:
        """Get an approval by its ID."""
        stmt = select(Approval).where(Approval.id == approval_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_pending(self, db: AsyncSession) -> list[Approval]:
        """Get all pending approvals."""
        stmt = (
            select(Approval)
            .where(Approval.status == "pending")
            .order_by(Approval.requested_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_expired(self, db: AsyncSession) -> list[Approval]:
        """Get all expired pending approvals."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(Approval)
            .where(Approval.status == "pending")
            .where(Approval.expires_at < now)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        db: AsyncSession,
        approval_id: UUID,
        status: str,
        approved_by: str | None = None,
        responded_at: datetime | None = None,
    ) -> Approval | None:
        """Update approval status."""
        stmt = (
            update(Approval)
            .where(Approval.id == approval_id)
            .values(
                status=status,
                approved_by=approved_by,
                responded_at=responded_at or datetime.now(timezone.utc),
            )
            .returning(Approval)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_expired(self, db: AsyncSession, approval_id: UUID) -> Approval | None:
        """Mark an approval as expired."""
        return await self.update_status(
            db,
            approval_id,
            status="expired",
            responded_at=datetime.now(timezone.utc),
        )

    async def set_telegram_message_id(
        self, db: AsyncSession, approval_id: UUID, message_id: str
    ) -> None:
        """Update the Telegram message ID for an approval."""
        stmt = (
            update(Approval)
            .where(Approval.id == approval_id)
            .values(telegram_message_id=message_id)
        )
        await db.execute(stmt)


# Singleton instance
approval_repository = ApprovalRepository()
