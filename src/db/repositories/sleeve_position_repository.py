"""
Repository for SleevePosition (virtual ledger) database operations.

The sleeve positions table is the source of truth for what positions
belong to each sleeve, independent of broker holdings.
"""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SleevePosition


class SleevePositionRepository:
    """Data access layer for sleeve positions (virtual ledger)."""

    async def get_by_sleeve(
        self, db: AsyncSession, sleeve_id: UUID
    ) -> list[SleevePosition]:
        """Get all positions for a sleeve."""
        stmt = (
            select(SleevePosition)
            .where(SleevePosition.sleeve_id == sleeve_id)
            .order_by(SleevePosition.symbol)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_position(
        self, db: AsyncSession, sleeve_id: UUID, symbol: str
    ) -> SleevePosition | None:
        """Get a specific position by sleeve and symbol."""
        stmt = select(SleevePosition).where(
            SleevePosition.sleeve_id == sleeve_id,
            SleevePosition.symbol == symbol.upper(),
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_position_map(
        self, db: AsyncSession, sleeve_id: UUID
    ) -> dict[str, SleevePosition]:
        """Get all positions as a symbol -> position dict."""
        positions = await self.get_by_sleeve(db, sleeve_id)
        return {p.symbol: p for p in positions}

    async def create_position(
        self,
        db: AsyncSession,
        sleeve_id: UUID,
        symbol: str,
        shares: Decimal,
        weight: Decimal | None = None,
        cost_basis: Decimal | None = None,
    ) -> SleevePosition:
        """Create a new position in the ledger."""
        position = SleevePosition(
            sleeve_id=sleeve_id,
            symbol=symbol.upper(),
            shares=shares,
            weight=weight,
            cost_basis=cost_basis,
            last_trade_at=datetime.now(timezone.utc),
        )
        db.add(position)
        await db.flush()
        return position

    async def update_position(
        self,
        db: AsyncSession,
        position: SleevePosition,
        shares: Decimal | None = None,
        weight: Decimal | None = None,
        cost_basis: Decimal | None = None,
    ) -> SleevePosition:
        """Update an existing position."""
        if shares is not None:
            position.shares = shares
        if weight is not None:
            position.weight = weight
        if cost_basis is not None:
            position.cost_basis = cost_basis
        position.last_trade_at = datetime.now(timezone.utc)
        await db.flush()
        return position

    async def upsert_position(
        self,
        db: AsyncSession,
        sleeve_id: UUID,
        symbol: str,
        shares: Decimal,
        weight: Decimal | None = None,
        cost_basis: Decimal | None = None,
    ) -> SleevePosition:
        """Create or update a position."""
        existing = await self.get_position(db, sleeve_id, symbol)
        if existing:
            return await self.update_position(
                db, existing, shares=shares, weight=weight, cost_basis=cost_basis
            )
        else:
            return await self.create_position(
                db, sleeve_id, symbol, shares, weight, cost_basis
            )

    async def add_shares(
        self,
        db: AsyncSession,
        sleeve_id: UUID,
        symbol: str,
        shares_delta: Decimal,
        weight: Decimal | None = None,
        cost_delta: Decimal | None = None,
    ) -> SleevePosition:
        """Add shares to a position (creates if doesn't exist)."""
        existing = await self.get_position(db, sleeve_id, symbol)
        if existing:
            new_shares = existing.shares + shares_delta
            new_cost = (
                (existing.cost_basis or Decimal(0)) + (cost_delta or Decimal(0))
                if cost_delta
                else existing.cost_basis
            )
            return await self.update_position(
                db, existing, shares=new_shares, weight=weight, cost_basis=new_cost
            )
        else:
            return await self.create_position(
                db, sleeve_id, symbol, shares_delta, weight, cost_delta
            )

    async def remove_shares(
        self,
        db: AsyncSession,
        sleeve_id: UUID,
        symbol: str,
        shares_delta: Decimal,
    ) -> SleevePosition | None:
        """
        Remove shares from a position.
        Deletes the position if shares go to zero or below.
        Returns None if position was deleted.
        """
        existing = await self.get_position(db, sleeve_id, symbol)
        if not existing:
            return None

        new_shares = existing.shares - shares_delta
        if new_shares <= 0:
            await self.delete_position(db, sleeve_id, symbol)
            return None
        else:
            # Proportionally reduce cost basis
            if existing.cost_basis and existing.shares > 0:
                ratio = new_shares / existing.shares
                new_cost = existing.cost_basis * ratio
            else:
                new_cost = existing.cost_basis
            return await self.update_position(
                db, existing, shares=new_shares, cost_basis=new_cost
            )

    async def delete_position(
        self, db: AsyncSession, sleeve_id: UUID, symbol: str
    ) -> bool:
        """Delete a position entirely (full exit)."""
        stmt = delete(SleevePosition).where(
            SleevePosition.sleeve_id == sleeve_id,
            SleevePosition.symbol == symbol.upper(),
        )
        result = await db.execute(stmt)
        return result.rowcount > 0

    async def get_total_value(
        self, db: AsyncSession, sleeve_id: UUID, prices: dict[str, Decimal]
    ) -> Decimal:
        """
        Calculate total sleeve value given current prices.
        prices: dict of symbol -> current price
        """
        positions = await self.get_by_sleeve(db, sleeve_id)
        total = Decimal(0)
        for pos in positions:
            price = prices.get(pos.symbol, Decimal(0))
            total += pos.shares * price
        return total


# Singleton instance
sleeve_position_repository = SleevePositionRepository()
