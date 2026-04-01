"""
Repository for Reconciliation and PortfolioIntent database operations.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import PortfolioIntent, Reconciliation, Signal


class ReconciliationRepository:
    """Data access layer for reconciliations and intents."""

    async def create_signal(
        self,
        db: AsyncSession,
        sleeve_id: UUID,
        source_event_id: str,
        event_type: str,
        detected_at: datetime | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> Signal:
        """Create a signal record."""
        signal = Signal(
            sleeve_id=sleeve_id,
            source_event_id=source_event_id,
            event_type=event_type,
            detected_at=detected_at or datetime.now(timezone.utc),
            raw_payload=raw_payload,
            status="processing",
        )
        db.add(signal)
        await db.flush()
        return signal

    async def create_intent(
        self,
        db: AsyncSession,
        signal_id: UUID,
        sleeve_id: UUID,
        target_allocations: list[dict[str, Any]],
        intent_type: str = "full_rebalance",
        confidence: Decimal | None = None,
    ) -> PortfolioIntent:
        """Create a portfolio intent record."""
        intent = PortfolioIntent(
            signal_id=signal_id,
            sleeve_id=sleeve_id,
            target_allocations=target_allocations,
            intent_type=intent_type,
            confidence=confidence,
            requires_review=False,
        )
        db.add(intent)
        await db.flush()
        return intent

    async def create_reconciliation(
        self,
        db: AsyncSession,
        intent_id: UUID,
        sleeve_id: UUID,
        holdings_snapshot: dict[str, Any],
        proposed_trades: list[dict[str, Any]],
        result_type: str = "proposed",
        review_reason: str | None = None,
    ) -> Reconciliation:
        """Create a reconciliation record."""
        recon = Reconciliation(
            intent_id=intent_id,
            sleeve_id=sleeve_id,
            holdings_snapshot=holdings_snapshot,
            proposed_trades=proposed_trades,
            result_type=result_type,
            review_reason=review_reason,
        )
        db.add(recon)
        await db.flush()
        return recon

    async def create_full_chain(
        self,
        db: AsyncSession,
        sleeve_id: UUID,
        source_event_id: str,
        event_type: str,
        proposed_trades: list[dict[str, Any]],
        holdings_snapshot: dict[str, Any] | None = None,
        target_allocations: list[dict[str, Any]] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> tuple[Signal, PortfolioIntent, Reconciliation]:
        """
        Create the full Signal → Intent → Reconciliation chain in one transaction.

        This is a convenience method for when you need to persist all three
        records atomically before creating an approval.

        Returns:
            Tuple of (Signal, PortfolioIntent, Reconciliation)
        """
        # Create signal
        signal = await self.create_signal(
            db=db,
            sleeve_id=sleeve_id,
            source_event_id=source_event_id,
            event_type=event_type,
            raw_payload=raw_payload,
        )

        # Create intent (use trades as target allocations if not provided)
        intent = await self.create_intent(
            db=db,
            signal_id=signal.id,
            sleeve_id=sleeve_id,
            target_allocations=target_allocations or proposed_trades,
            intent_type="full_rebalance",
        )

        # Create reconciliation
        recon = await self.create_reconciliation(
            db=db,
            intent_id=intent.id,
            sleeve_id=sleeve_id,
            holdings_snapshot=holdings_snapshot or {},
            proposed_trades=proposed_trades,
            result_type="proposed",
        )

        return signal, intent, recon

    async def get_reconciliation_by_id(
        self, db: AsyncSession, recon_id: UUID
    ) -> Reconciliation | None:
        """Get a reconciliation by ID."""
        stmt = select(Reconciliation).where(Reconciliation.id == recon_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_recent_reconciliations(
        self, db: AsyncSession, sleeve_id: UUID | None = None, limit: int = 20
    ) -> list[Reconciliation]:
        """Get recent reconciliations."""
        stmt = select(Reconciliation).order_by(Reconciliation.created_at.desc()).limit(limit)
        if sleeve_id:
            stmt = stmt.where(Reconciliation.sleeve_id == sleeve_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())


# Singleton instance
reconciliation_repository = ReconciliationRepository()
