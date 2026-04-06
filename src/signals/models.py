"""
Domain models for signal processing.

These are the runtime dataclasses used throughout the signal processing pipeline.
They map to the database models but are optimized for in-memory processing.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class SignalStatus(str, Enum):
    """Status of a signal in the processing pipeline."""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


class IntentType(str, Enum):
    """Type of portfolio intent."""

    FULL_REBALANCE = "full_rebalance"
    PARTIAL_UPDATE = "partial_update"


class ReviewStatus(str, Enum):
    """Status of manual review."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReconciliationResult(str, Enum):
    """Result type from reconciliation."""

    NO_ACTION = "no_action"
    PROPOSED = "proposed"
    MANUAL_REVIEW = "manual_review"


@dataclass
class TargetAllocation:
    """
    A single target allocation within a portfolio intent.

    Represents the desired state for one position.
    """

    symbol: str
    target_weight: float  # 0.0 to 1.0
    side: str  # 'long' or 'short'

    # Optional metadata from source
    raw_weight: int | None = None
    asset_name: str | None = None
    category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "symbol": self.symbol,
            "target_weight": self.target_weight,
            "side": self.side,
            "raw_weight": self.raw_weight,
            "asset_name": self.asset_name,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetAllocation":
        """Create from dictionary."""
        return cls(
            symbol=data["symbol"],
            target_weight=data["target_weight"],
            side=data.get("side", "long"),
            raw_weight=data.get("raw_weight"),
            asset_name=data.get("asset_name"),
            category=data.get("category"),
        )


@dataclass
class Signal:
    """
    A trigger event detected from a source.

    Represents "we detected something happened" - the raw event
    that triggers further processing.
    """

    id: UUID
    sleeve_id: UUID
    source_event_id: str  # Idempotency key (e.g., email message ID)
    event_type: str  # 'email_detected', 'scheduled_check', 'manual_trigger'
    detected_at: datetime

    status: SignalStatus = SignalStatus.PENDING
    raw_payload: dict[str, Any] | None = None
    processed_at: datetime | None = None
    error_message: str | None = None

    @classmethod
    def create(
        cls,
        sleeve_id: UUID,
        source_event_id: str,
        event_type: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> "Signal":
        """Factory method to create a new signal."""
        return cls(
            id=uuid4(),
            sleeve_id=sleeve_id,
            source_event_id=source_event_id,
            event_type=event_type,
            detected_at=datetime.now(),
            raw_payload=raw_payload,
        )


@dataclass
class PortfolioIntent:
    """
    Interpreted target state for a sleeve.

    Represents "here's what the sleeve should look like" after
    processing a signal. This is the normalized, validated
    interpretation of the source data.
    """

    id: UUID
    signal_id: UUID
    sleeve_id: UUID
    target_allocations: list[TargetAllocation]
    intent_type: IntentType
    confidence: float  # 0.0 to 1.0

    # Manual review workflow
    requires_review: bool = False
    review_reason: str | None = None
    review_status: ReviewStatus | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None

    created_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def create(
        cls,
        signal_id: UUID,
        sleeve_id: UUID,
        target_allocations: list[TargetAllocation],
        intent_type: IntentType = IntentType.FULL_REBALANCE,
        confidence: float = 1.0,
    ) -> "PortfolioIntent":
        """Factory method to create a new intent."""
        return cls(
            id=uuid4(),
            signal_id=signal_id,
            sleeve_id=sleeve_id,
            target_allocations=target_allocations,
            intent_type=intent_type,
            confidence=confidence,
        )

    def flag_for_review(self, reason: str):
        """Flag this intent for manual review."""
        self.requires_review = True
        self.review_reason = reason
        self.review_status = ReviewStatus.PENDING_REVIEW

    def allocations_to_dict(self) -> list[dict[str, Any]]:
        """Convert allocations to list of dicts for JSON serialization."""
        return [a.to_dict() for a in self.target_allocations]

    @property
    def total_weight(self) -> float:
        """Sum of all target weights."""
        return sum(a.target_weight for a in self.target_allocations)

    @property
    def position_count(self) -> int:
        """Number of positions in the intent."""
        return len(self.target_allocations)


@dataclass
class ProposedTrade:
    """
    A single proposed trade from reconciliation.
    """

    symbol: str
    side: str  # 'buy' or 'sell'
    notional: float  # Dollar amount
    quantity: float | None = None  # Shares (if known)

    # Context
    current_weight: float = 0.0
    target_weight: float = 0.0
    delta_weight: float = 0.0
    rationale: str = ""

    # Price context (captured at proposal time)
    proposal_price: float | None = None  # Market price when trade was proposed
    bravos_entry_price: float | None = None  # Bravos's original entry price (if available)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "notional": self.notional,
            "quantity": self.quantity,
            "current_weight": self.current_weight,
            "target_weight": self.target_weight,
            "weight_delta": self.delta_weight,  # Map to expected key for ledger updates
            "rationale": self.rationale,
            "proposal_price": self.proposal_price,
            "bravos_entry_price": self.bravos_entry_price,
        }


@dataclass
class ReconciliationPlan:
    """
    The computed trades needed to move from current holdings to target intent.

    Represents "here's how to get there" - the actual trade deltas.
    """

    id: UUID
    intent_id: UUID
    sleeve_id: UUID
    holdings_snapshot: dict[str, Any]  # Current broker holdings
    proposed_trades: list[ProposedTrade]
    result_type: ReconciliationResult

    # If manual review needed
    review_reason: str | None = None

    created_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def create(
        cls,
        intent_id: UUID,
        sleeve_id: UUID,
        holdings_snapshot: dict[str, Any],
        proposed_trades: list[ProposedTrade],
        result_type: ReconciliationResult,
        review_reason: str | None = None,
    ) -> "ReconciliationPlan":
        """Factory method to create a new reconciliation plan."""
        return cls(
            id=uuid4(),
            intent_id=intent_id,
            sleeve_id=sleeve_id,
            holdings_snapshot=holdings_snapshot,
            proposed_trades=proposed_trades,
            result_type=result_type,
            review_reason=review_reason,
        )

    def trades_to_dict(self) -> list[dict[str, Any]]:
        """Convert trades to list of dicts for JSON serialization."""
        return [t.to_dict() for t in self.proposed_trades]

    @property
    def total_notional(self) -> float:
        """Total dollar amount of all proposed trades."""
        return sum(abs(t.notional) for t in self.proposed_trades)

    @property
    def trade_count(self) -> int:
        """Number of proposed trades."""
        return len(self.proposed_trades)

    @property
    def has_trades(self) -> bool:
        """Whether there are any trades to execute."""
        return len(self.proposed_trades) > 0
