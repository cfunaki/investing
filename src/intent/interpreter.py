"""
Intent interpreter - converts raw portfolio data to PortfolioIntent.

The interpreter's job is to:
1. Take the raw portfolio snapshot from an adapter
2. Convert it to a normalized PortfolioIntent
3. Add metadata about confidence and type
"""

from uuid import UUID

import structlog

from src.adapters.base import Allocation as AdapterAllocation
from src.adapters.base import PortfolioSnapshot
from src.signals.models import IntentType, PortfolioIntent, TargetAllocation

logger = structlog.get_logger(__name__)


class IntentInterpreter:
    """
    Interprets portfolio snapshots into portfolio intents.

    This is where we convert the raw adapter output into
    the canonical format used by the rest of the system.
    """

    def interpret(
        self,
        signal_id: UUID,
        sleeve_id: UUID,
        snapshot: PortfolioSnapshot,
    ) -> PortfolioIntent:
        """
        Interpret a portfolio snapshot into a portfolio intent.

        Args:
            signal_id: ID of the signal that triggered this
            sleeve_id: ID of the sleeve
            snapshot: Raw portfolio data from adapter

        Returns:
            PortfolioIntent with normalized allocations
        """
        log = logger.bind(
            signal_id=str(signal_id),
            sleeve=snapshot.sleeve_name,
            positions=snapshot.total_positions,
        )

        log.info("interpreting_snapshot")

        # Convert adapter allocations to target allocations
        target_allocations = self._convert_allocations(snapshot.allocations)

        # Determine intent type
        # For now, we assume all Bravos updates are full rebalances
        # Future sleeves might have partial updates
        intent_type = self._determine_intent_type(snapshot)

        # Calculate confidence based on data quality
        confidence = self._calculate_confidence(snapshot, target_allocations)

        intent = PortfolioIntent.create(
            signal_id=signal_id,
            sleeve_id=sleeve_id,
            target_allocations=target_allocations,
            intent_type=intent_type,
            confidence=confidence,
        )

        log.info(
            "intent_created",
            intent_id=str(intent.id),
            positions=intent.position_count,
            total_weight=intent.total_weight,
            confidence=confidence,
        )

        return intent

    def _convert_allocations(
        self, adapter_allocations: list[AdapterAllocation]
    ) -> list[TargetAllocation]:
        """Convert adapter allocations to target allocations."""
        return [
            TargetAllocation(
                symbol=alloc.symbol,
                target_weight=alloc.target_weight,
                side=alloc.side,
                raw_weight=alloc.raw_weight,
                asset_name=alloc.asset_name,
                category=alloc.category,
            )
            for alloc in adapter_allocations
        ]

    def _determine_intent_type(self, snapshot: PortfolioSnapshot) -> IntentType:
        """
        Determine the type of intent based on the snapshot.

        For now, we assume all updates are full rebalances.
        Future sleeves might have different semantics.
        """
        # Bravos always provides a complete portfolio
        return IntentType.FULL_REBALANCE

    def _calculate_confidence(
        self,
        snapshot: PortfolioSnapshot,
        allocations: list[TargetAllocation],
    ) -> float:
        """
        Calculate confidence score for this interpretation.

        Factors:
        - Whether weights sum to ~1.0
        - Whether we have a reasonable number of positions
        - Data freshness

        Returns:
            Confidence score from 0.0 to 1.0
        """
        confidence = 1.0

        # Check weight sum
        total_weight = sum(a.target_weight for a in allocations)
        if abs(total_weight - 1.0) > 0.05:
            # Weights don't sum to 100%, reduce confidence
            confidence *= 0.7

        # Check position count
        if len(allocations) == 0:
            confidence = 0.0
        elif len(allocations) < 3:
            # Suspiciously few positions
            confidence *= 0.8

        # Check for any short positions (unusual for Bravos)
        short_count = sum(1 for a in allocations if a.side == "short")
        if short_count > 0:
            # Bravos rarely has shorts, flag for review but don't reduce confidence too much
            confidence *= 0.9

        return round(confidence, 2)


# Singleton instance
_interpreter: IntentInterpreter | None = None


def get_interpreter() -> IntentInterpreter:
    """Get the intent interpreter singleton."""
    global _interpreter
    if _interpreter is None:
        _interpreter = IntentInterpreter()
    return _interpreter
