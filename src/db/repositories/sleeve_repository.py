"""
Repository for Sleeve database operations.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Sleeve


class SleeveRepository:
    """Data access layer for sleeves."""

    async def get_by_name(self, db: AsyncSession, name: str) -> Sleeve | None:
        """Get a sleeve by its name."""
        stmt = select(Sleeve).where(Sleeve.name == name)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, db: AsyncSession, sleeve_id: UUID) -> Sleeve | None:
        """Get a sleeve by its ID."""
        stmt = select(Sleeve).where(Sleeve.id == sleeve_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_enabled(self, db: AsyncSession) -> list[Sleeve]:
        """Get all enabled sleeves."""
        stmt = select(Sleeve).where(Sleeve.enabled == True).order_by(Sleeve.name)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_all(self, db: AsyncSession) -> list[Sleeve]:
        """Get all sleeves."""
        stmt = select(Sleeve).order_by(Sleeve.name)
        result = await db.execute(stmt)
        return list(result.scalars().all())


# Singleton instance
sleeve_repository = SleeveRepository()
