"""
SQLAlchemy ORM models for the investing automation platform.

These models map to the Postgres tables defined in schema.sql.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


# =============================================================================
# SLEEVES
# =============================================================================


class Sleeve(Base):
    """
    Investment strategy source configuration.

    Each sleeve represents a distinct investment strategy/source (e.g., Bravos).
    """

    __tablename__ = "sleeves"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    adapter_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'bravos_web', 'api', 'email_parse'
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Capital allocation
    allocation_mode: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'fixed_dollars', 'percent_of_equity', 'unit_based'
    allocation_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    unit_size: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=500.00
    )  # Dollar amount per weight unit
    cash_handling: Mapped[str] = mapped_column(
        String, default="sleeve_isolated"
    )  # 'sleeve_isolated', 'shared_pool'
    rebalance_priority: Mapped[int] = mapped_column(Integer, default=100)

    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    signals: Mapped[list["Signal"]] = relationship(back_populates="sleeve")
    intents: Mapped[list["PortfolioIntent"]] = relationship(back_populates="sleeve")
    reconciliations: Mapped[list["Reconciliation"]] = relationship(
        back_populates="sleeve"
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="sleeve")
    positions: Mapped[list["SleevePosition"]] = relationship(back_populates="sleeve")


# =============================================================================
# SLEEVE POSITIONS (Virtual Ledger)
# =============================================================================


class SleevePosition(Base):
    """
    Virtual ledger tracking positions per sleeve.

    This is the source of truth for sleeve composition, NOT broker holdings.
    Tracks shares (not notional) so values don't drift - only changes on trades.
    """

    __tablename__ = "sleeve_positions"
    __table_args__ = (
        Index("idx_sleeve_positions_sleeve", "sleeve_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    sleeve_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sleeves.id", ondelete="CASCADE")
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)

    # Position tracking
    shares: Mapped[Decimal] = mapped_column(
        Numeric(16, 6), nullable=False, default=0
    )
    weight: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 4)
    )  # Source weight (e.g., Bravos weight 5)
    cost_basis: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # Audit trail
    last_trade_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    sleeve: Mapped["Sleeve"] = relationship(back_populates="positions")


# =============================================================================
# SIGNALS
# =============================================================================


class Signal(Base):
    """
    Trigger event detected from a source.

    A signal is "we detected something happened" - it triggers further processing.
    """

    __tablename__ = "signals"
    __table_args__ = (
        Index("idx_signals_sleeve_status", "sleeve_id", "status"),
        Index("idx_signals_detected_at", "detected_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    sleeve_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sleeves.id", ondelete="CASCADE")
    )
    source_event_id: Mapped[str] = mapped_column(
        String, nullable=False
    )  # Idempotency key
    event_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'email_detected', 'scheduled_check'
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(
        String, default="pending"
    )  # 'pending', 'processing', 'processed', 'failed', 'skipped'
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    sleeve: Mapped["Sleeve"] = relationship(back_populates="signals")
    intents: Mapped[list["PortfolioIntent"]] = relationship(back_populates="signal")


# =============================================================================
# PORTFOLIO INTENTS
# =============================================================================


class PortfolioIntent(Base):
    """
    Interpreted target state for a sleeve after processing a signal.

    "Here's what the sleeve should look like."
    """

    __tablename__ = "portfolio_intents"
    __table_args__ = (
        Index(
            "idx_intents_review",
            "requires_review",
            "review_status",
            postgresql_where="requires_review = true",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    signal_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("signals.id", ondelete="CASCADE")
    )
    sleeve_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sleeves.id", ondelete="CASCADE")
    )

    target_allocations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )  # [{symbol, target_weight, side}, ...]
    intent_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'full_rebalance', 'partial_update'
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))  # 0.00 to 1.00

    # Manual review workflow
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reason: Mapped[Optional[str]] = mapped_column(Text)
    review_status: Mapped[Optional[str]] = mapped_column(
        String
    )  # 'pending_review', 'approved', 'rejected'
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(String)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    signal: Mapped["Signal"] = relationship(back_populates="intents")
    sleeve: Mapped["Sleeve"] = relationship(back_populates="intents")
    reconciliations: Mapped[list["Reconciliation"]] = relationship(
        back_populates="intent"
    )


# =============================================================================
# RECONCILIATIONS
# =============================================================================


class Reconciliation(Base):
    """
    Computed trade deltas to reach target state.

    "Here's how to get there."
    """

    __tablename__ = "reconciliations"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    intent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("portfolio_intents.id", ondelete="CASCADE")
    )
    sleeve_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sleeves.id", ondelete="CASCADE")
    )

    holdings_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )  # Broker state
    proposed_trades: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )  # [{symbol, side, notional, quantity}, ...]

    result_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'no_action', 'proposed', 'manual_review'
    review_reason: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    intent: Mapped["PortfolioIntent"] = relationship(back_populates="reconciliations")
    sleeve: Mapped["Sleeve"] = relationship(back_populates="reconciliations")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="reconciliation")


# =============================================================================
# APPROVALS
# =============================================================================


class Approval(Base):
    """
    Human authorization request for proposed trades.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index(
            "idx_approvals_status_expires",
            "status",
            "expires_at",
            postgresql_where="status = 'pending'",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    reconciliation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("reconciliations.id", ondelete="CASCADE")
    )

    approval_code: Mapped[str] = mapped_column(
        String, unique=True, nullable=False
    )  # Short code like 'a3f2'
    proposed_trades: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    telegram_message_id: Mapped[Optional[str]] = mapped_column(String)

    status: Mapped[str] = mapped_column(
        String, default="pending"
    )  # 'pending', 'approved', 'rejected', 'expired'
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[Optional[str]] = mapped_column(String)  # Telegram user ID

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    reconciliation: Mapped["Reconciliation"] = relationship(back_populates="approvals")
    executions: Mapped[list["Execution"]] = relationship(back_populates="approval")


# =============================================================================
# EXECUTIONS
# =============================================================================


class Execution(Base):
    """
    Individual order execution with broker.
    """

    __tablename__ = "executions"
    __table_args__ = (
        Index("idx_executions_status", "status"),
        Index("idx_executions_approval", "approval_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    approval_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("approvals.id", ondelete="CASCADE")
    )

    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)  # 'buy', 'sell'
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 6))
    notional: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # Idempotency at order level
    execution_key: Mapped[str] = mapped_column(
        String, unique=True, nullable=False
    )  # hash(approval_id, symbol, side)

    broker_order_id: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'pending', 'submitted', 'filled', 'partial', 'failed', 'cancelled'
    broker_response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    approval: Mapped["Approval"] = relationship(back_populates="executions")


# =============================================================================
# SNAPSHOTS
# =============================================================================


class Snapshot(Base):
    """
    Point-in-time portfolio snapshot for drift detection.
    """

    __tablename__ = "snapshots"
    __table_args__ = (Index("idx_snapshots_source_taken", "source", "taken_at"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    source: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'robinhood', 'bravos', etc.
    sleeve_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sleeves.id", ondelete="SET NULL")
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    sleeve: Mapped[Optional["Sleeve"]] = relationship(back_populates="snapshots")


# =============================================================================
# IDEMPOTENCY KEYS
# =============================================================================


class IdempotencyKey(Base):
    """
    Track processed operations to prevent duplicates.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index(
            "idx_idempotency_expires",
            "expires_at",
            postgresql_where="expires_at IS NOT NULL",
        ),
    )

    key: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'signal', 'intent', 'reconciliation', 'execution'
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
