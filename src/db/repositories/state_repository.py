"""
Repository for state persistence operations.

Covers key_value_state, entry_prices, and queued_executions tables.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import EntryPrice, KeyValueState, QueuedExecution


class StateRepository:
    """Data access layer for runtime state."""

    # ── key_value_state ──────────────────────────────────────────────

    async def get_state(self, db: AsyncSession, key: str) -> dict[str, Any] | None:
        """Get a state value by key."""
        stmt = select(KeyValueState.value).where(KeyValueState.key == key)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        return row

    async def set_state(self, db: AsyncSession, key: str, value: dict[str, Any]) -> None:
        """Set a state value (upsert)."""
        stmt = pg_insert(KeyValueState).values(
            key=key,
            value=value,
            updated_at=datetime.now(timezone.utc),
        ).on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": value,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        await db.execute(stmt)

    # ── entry_prices ─────────────────────────────────────────────────

    async def get_entry_price(self, db: AsyncSession, symbol: str) -> float | None:
        """Get entry price for a symbol."""
        stmt = select(EntryPrice.price).where(EntryPrice.symbol == symbol.upper())
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_entry_prices(self, db: AsyncSession) -> dict[str, float]:
        """Get all entry prices as a dict."""
        stmt = select(EntryPrice.symbol, EntryPrice.price)
        result = await db.execute(stmt)
        return {row[0]: float(row[1]) for row in result.all()}

    async def upsert_entry_price(
        self, db: AsyncSession, symbol: str, price: float, source: str
    ) -> bool:
        """Insert entry price only if not already present. Returns True if inserted."""
        stmt = pg_insert(EntryPrice).values(
            symbol=symbol.upper(),
            price=price,
            source=source,
        ).on_conflict_do_nothing(index_elements=["symbol"])
        result = await db.execute(stmt)
        return result.rowcount > 0

    # ── queued_executions ────────────────────────────────────────────

    async def queue_execution(
        self,
        db: AsyncSession,
        approval_id: UUID,
        trades: list[dict[str, Any]],
        execute_after: datetime,
    ) -> QueuedExecution:
        """Queue trades for later execution."""
        record = QueuedExecution(
            approval_id=approval_id,
            trades=trades,
            execute_after=execute_after,
            status="pending",
        )
        db.add(record)
        await db.flush()
        return record

    async def get_pending_executions(
        self, db: AsyncSession, before: datetime | None = None
    ) -> list[QueuedExecution]:
        """Get pending queued executions ready to run."""
        stmt = select(QueuedExecution).where(
            QueuedExecution.status == "pending",
        )
        if before:
            stmt = stmt.where(QueuedExecution.execute_after <= before)
        stmt = stmt.order_by(QueuedExecution.queued_at)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def update_queued_status(
        self,
        db: AsyncSession,
        queued_id: UUID,
        status: str,
        error: str | None = None,
    ) -> None:
        """Update queued execution status."""
        values: dict[str, Any] = {"status": status}
        if status in ("completed", "failed"):
            values["executed_at"] = datetime.now(timezone.utc)
        if error:
            values["error"] = error
        stmt = update(QueuedExecution).where(
            QueuedExecution.id == queued_id,
        ).values(**values)
        await db.execute(stmt)

    async def cancel_pending_executions(self, db: AsyncSession) -> int:
        """Cancel all pending queued executions. Returns count cancelled."""
        stmt = update(QueuedExecution).where(
            QueuedExecution.status == "pending",
        ).values(status="cancelled")
        result = await db.execute(stmt)
        return result.rowcount


state_repository = StateRepository()
