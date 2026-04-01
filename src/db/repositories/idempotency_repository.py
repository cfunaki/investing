"""
Repository for IdempotencyKey database operations.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import IdempotencyKey


class IdempotencyRepository:
    """Data access layer for idempotency keys."""

    async def get(self, db: AsyncSession, key: str) -> IdempotencyKey | None:
        """Get an idempotency key record."""
        stmt = select(IdempotencyKey).where(IdempotencyKey.key == key)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def exists(self, db: AsyncSession, key: str) -> bool:
        """Check if an idempotency key exists."""
        record = await self.get(db, key)
        if record is None:
            return False
        # Check if expired
        if record.expires_at and record.expires_at < datetime.now(timezone.utc):
            return False
        return True

    async def create(
        self,
        db: AsyncSession,
        key: str,
        scope: str,
        result: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> IdempotencyKey:
        """Create a new idempotency key."""
        record = IdempotencyKey(
            key=key,
            scope=scope,
            result=result,
            expires_at=expires_at,
        )
        db.add(record)
        await db.flush()
        return record

    async def get_or_create(
        self,
        db: AsyncSession,
        key: str,
        scope: str,
        result: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[IdempotencyKey, bool]:
        """
        Get an existing key or create a new one.

        Returns:
            Tuple of (record, created) where created is True if new.
        """
        existing = await self.get(db, key)
        if existing:
            # Check if expired
            if existing.expires_at and existing.expires_at < datetime.now(timezone.utc):
                # Delete expired and create new
                await db.delete(existing)
                await db.flush()
            else:
                return existing, False

        record = await self.create(db, key, scope, result, expires_at)
        return record, True

    async def update_result(
        self,
        db: AsyncSession,
        key: str,
        result: dict[str, Any],
    ) -> IdempotencyKey | None:
        """Update the result for an idempotency key."""
        record = await self.get(db, key)
        if record:
            record.result = result
            await db.flush()
        return record

    async def delete_expired(self, db: AsyncSession) -> int:
        """Delete all expired idempotency keys. Returns count deleted."""
        now = datetime.now(timezone.utc)
        stmt = (
            delete(IdempotencyKey)
            .where(IdempotencyKey.expires_at.isnot(None))
            .where(IdempotencyKey.expires_at < now)
        )
        result = await db.execute(stmt)
        return result.rowcount


# Singleton instance
idempotency_repository = IdempotencyRepository()
