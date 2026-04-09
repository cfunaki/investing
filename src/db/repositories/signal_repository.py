"""
Repository for Signal database operations.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Signal


class SignalRepository:
    """Data access layer for signals."""

    async def create(
        self,
        db: AsyncSession,
        sleeve_id: UUID,
        source_event_id: str,
        event_type: str,
        detected_at: datetime,
        raw_payload: dict[str, Any] | None = None,
        status: str = "pending",
    ) -> Signal:
        """Create a new signal."""
        signal = Signal(
            sleeve_id=sleeve_id,
            source_event_id=source_event_id,
            event_type=event_type,
            detected_at=detected_at,
            raw_payload=raw_payload,
            status=status,
        )
        db.add(signal)
        await db.flush()
        return signal

    async def get_by_source_event_id(
        self, db: AsyncSession, sleeve_id: UUID, source_event_id: str
    ) -> Signal | None:
        """Get a signal by its source event ID (idempotency check)."""
        stmt = select(Signal).where(
            Signal.sleeve_id == sleeve_id,
            Signal.source_event_id == source_event_id,
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def exists(
        self, db: AsyncSession, sleeve_id: UUID, source_event_id: str
    ) -> bool:
        """Check if a signal already exists for this source event."""
        signal = await self.get_by_source_event_id(db, sleeve_id, source_event_id)
        return signal is not None

    async def get_by_id(self, db: AsyncSession, signal_id: UUID) -> Signal | None:
        """Get a signal by its ID."""
        stmt = select(Signal).where(Signal.id == signal_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_processed_event_ids(
        self, db: AsyncSession, sleeve_id: UUID
    ) -> set[str]:
        """Get all processed source event IDs for a sleeve."""
        stmt = select(Signal.source_event_id).where(
            Signal.sleeve_id == sleeve_id,
            Signal.status.in_(["processed", "processing", "skipped"]),
        )
        result = await db.execute(stmt)
        return {row[0] for row in result.all()}

    async def get_last_processed(
        self, db: AsyncSession, sleeve_id: UUID
    ) -> Signal | None:
        """Get the most recently processed signal for a sleeve."""
        stmt = (
            select(Signal)
            .where(
                Signal.sleeve_id == sleeve_id,
                Signal.status == "processed",
            )
            .order_by(Signal.processed_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        db: AsyncSession,
        signal_id: UUID,
        status: str,
        error_message: str | None = None,
        processed_at: datetime | None = None,
    ) -> Signal | None:
        """Update signal status."""
        values = {"status": status}
        if error_message is not None:
            values["error_message"] = error_message
        if processed_at is not None:
            values["processed_at"] = processed_at

        stmt = (
            update(Signal)
            .where(Signal.id == signal_id)
            .values(**values)
            .returning(Signal)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def increment_retry(
        self,
        db: AsyncSession,
        signal_id: UUID,
    ) -> int:
        """Increment retry count and return new count."""
        stmt = (
            update(Signal)
            .where(Signal.id == signal_id)
            .values(
                retry_count=Signal.retry_count + 1,
                status="retry",
            )
            .returning(Signal.retry_count)
        )
        result = await db.execute(stmt)
        return result.scalar_one()

    async def get_recent(
        self, db: AsyncSession, sleeve_id: UUID | None = None, limit: int = 20
    ) -> list[Signal]:
        """Get recent signals, optionally filtered by sleeve."""
        stmt = select(Signal).order_by(Signal.detected_at.desc()).limit(limit)
        if sleeve_id:
            stmt = stmt.where(Signal.sleeve_id == sleeve_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())


# Singleton instance
signal_repository = SignalRepository()
