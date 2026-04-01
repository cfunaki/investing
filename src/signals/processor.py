"""
Signal processor - orchestrates the full processing pipeline.

The processor handles:
1. Creating signals from trigger events
2. Fetching portfolio data via adapters
3. Interpreting into portfolio intents
4. Validating intents
5. (Future) Triggering reconciliation

This is the main entry point for signal processing.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from src.adapters.base import AdapterError, PortfolioSnapshot, SleeveAdapter
from src.adapters.bravos_web import BravosWebAdapter
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context
from src.intent.interpreter import IntentInterpreter, get_interpreter
from src.intent.validators import IntentValidationPipeline, get_validation_pipeline
from src.signals.models import (
    IntentType,
    PortfolioIntent,
    Signal,
    SignalStatus,
    TargetAllocation,
)

logger = structlog.get_logger(__name__)


@dataclass
class ProcessingResult:
    """Result from processing a signal."""

    success: bool
    signal: Signal
    intent: PortfolioIntent | None = None
    error: str | None = None
    error_type: str | None = None

    # Validation info
    validation_passed: bool = True
    validation_issues: list[str] | None = None

    # Timing
    processing_time_ms: int = 0
    fetch_time_ms: int = 0


class SignalProcessor:
    """
    Orchestrates the signal processing pipeline.

    Pipeline stages:
    1. Signal creation (idempotent)
    2. Portfolio fetch via adapter
    3. Intent interpretation
    4. Intent validation
    5. (Future) Store in database
    6. (Future) Trigger reconciliation
    """

    def __init__(
        self,
        interpreter: IntentInterpreter | None = None,
        validator: IntentValidationPipeline | None = None,
    ):
        """
        Initialize the processor.

        Args:
            interpreter: Intent interpreter (uses default if None)
            validator: Validation pipeline (uses default if None)
        """
        self.interpreter = interpreter or get_interpreter()
        self.validator = validator or get_validation_pipeline()

        # Adapter registry - for now just Bravos
        self._adapters: dict[str, SleeveAdapter] = {}

    def get_adapter(self, sleeve_name: str) -> SleeveAdapter | None:
        """Get or create an adapter for a sleeve."""
        if sleeve_name not in self._adapters:
            if sleeve_name == "bravos":
                self._adapters[sleeve_name] = BravosWebAdapter()
            else:
                return None

        return self._adapters[sleeve_name]

    async def process_signal(
        self,
        sleeve_id: UUID,
        sleeve_name: str,
        source_event_id: str,
        event_type: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> ProcessingResult:
        """
        Process a signal through the full pipeline.

        Args:
            sleeve_id: UUID of the sleeve
            sleeve_name: Name of the sleeve (e.g., 'bravos')
            source_event_id: Idempotency key (e.g., email message ID)
            event_type: Type of event (e.g., 'email_detected')
            raw_payload: Optional raw data from the trigger

        Returns:
            ProcessingResult with signal, intent, and status
        """
        import time

        start_time = time.time()

        log = logger.bind(
            sleeve=sleeve_name,
            source_event_id=source_event_id,
            event_type=event_type,
        )

        log.info("signal_processing_started")

        # Create signal
        signal = Signal.create(
            sleeve_id=sleeve_id,
            source_event_id=source_event_id,
            event_type=event_type,
            raw_payload=raw_payload,
        )
        signal.status = SignalStatus.PROCESSING

        try:
            # Get adapter for this sleeve
            adapter = self.get_adapter(sleeve_name)
            if adapter is None:
                signal.status = SignalStatus.FAILED
                signal.error_message = f"No adapter found for sleeve: {sleeve_name}"
                return ProcessingResult(
                    success=False,
                    signal=signal,
                    error=signal.error_message,
                    error_type="config",
                )

            # Fetch portfolio data
            fetch_start = time.time()
            fetch_result = await adapter.fetch_portfolio()
            fetch_time_ms = int((time.time() - fetch_start) * 1000)

            if isinstance(fetch_result, AdapterError):
                signal.status = SignalStatus.FAILED
                signal.error_message = fetch_result.message
                log.error(
                    "signal_fetch_failed",
                    error_type=fetch_result.error_type,
                    error=fetch_result.message,
                )
                return ProcessingResult(
                    success=False,
                    signal=signal,
                    error=fetch_result.message,
                    error_type=fetch_result.error_type,
                    fetch_time_ms=fetch_time_ms,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            snapshot: PortfolioSnapshot = fetch_result

            # Interpret into intent
            intent = self.interpreter.interpret(
                signal_id=signal.id,
                sleeve_id=sleeve_id,
                snapshot=snapshot,
            )

            # Validate intent
            validation_passed, validation_issues = self.validator.validate_and_flag(intent)

            # Mark signal as processed
            signal.status = SignalStatus.PROCESSED
            signal.processed_at = datetime.now(timezone.utc)

            processing_time_ms = int((time.time() - start_time) * 1000)

            log.info(
                "signal_processing_completed",
                signal_id=str(signal.id),
                intent_id=str(intent.id),
                positions=intent.position_count,
                validation_passed=validation_passed,
                requires_review=intent.requires_review,
                processing_time_ms=processing_time_ms,
                fetch_time_ms=fetch_time_ms,
            )

            return ProcessingResult(
                success=True,
                signal=signal,
                intent=intent,
                validation_passed=validation_passed,
                validation_issues=validation_issues,
                processing_time_ms=processing_time_ms,
                fetch_time_ms=fetch_time_ms,
            )

        except Exception as e:
            signal.status = SignalStatus.FAILED
            signal.error_message = str(e)
            processing_time_ms = int((time.time() - start_time) * 1000)

            log.exception(
                "signal_processing_failed",
                error=str(e),
                processing_time_ms=processing_time_ms,
            )

            return ProcessingResult(
                success=False,
                signal=signal,
                error=str(e),
                error_type="unknown",
                processing_time_ms=processing_time_ms,
            )

    async def process_bravos_email(
        self,
        email_message_id: str,
        email_payload: dict[str, Any] | None = None,
    ) -> ProcessingResult:
        """
        Convenience method to process a Bravos email signal.

        Args:
            email_message_id: Gmail message ID (used as idempotency key)
            email_payload: Optional email metadata

        Returns:
            ProcessingResult
        """
        # Get sleeve_id from database
        sleeve_id = await self._get_sleeve_id("bravos")
        if not sleeve_id:
            return ProcessingResult(
                success=False,
                signal=Signal.create("bravos", email_message_id, "email_detected"),
                error="Bravos sleeve not found in database",
                error_type="config_error",
            )

        return await self.process_signal(
            sleeve_id=sleeve_id,
            sleeve_name="bravos",
            source_event_id=email_message_id,
            event_type="email_detected",
            raw_payload=email_payload,
        )

    async def _get_sleeve_id(self, sleeve_name: str) -> UUID | None:
        """Get sleeve ID from database by name."""
        try:
            async with get_db_context() as db:
                sleeve = await sleeve_repository.get_by_name(db, sleeve_name)
                if sleeve:
                    return sleeve.id
        except Exception as e:
            logger.warning("failed_to_get_sleeve_id", sleeve=sleeve_name, error=str(e))
        return None

    async def process_scheduled_check(
        self,
        sleeve_name: str,
        check_id: str | None = None,
    ) -> ProcessingResult:
        """
        Process a scheduled reconciliation check.

        Args:
            sleeve_name: Name of the sleeve to check
            check_id: Optional unique ID for this check

        Returns:
            ProcessingResult
        """
        from datetime import datetime

        # Generate a unique check ID if not provided
        if check_id is None:
            check_id = f"scheduled_{datetime.now(timezone.utc).isoformat()}"

        # Get sleeve_id from database
        sleeve_id = await self._get_sleeve_id(sleeve_name)
        if not sleeve_id:
            return ProcessingResult(
                success=False,
                signal=Signal.create(sleeve_name, check_id, "scheduled_check"),
                error=f"Sleeve '{sleeve_name}' not found in database",
                error_type="config_error",
            )

        return await self.process_signal(
            sleeve_id=sleeve_id,
            sleeve_name=sleeve_name,
            source_event_id=check_id,
            event_type="scheduled_check",
        )

    async def close(self):
        """Close all adapters."""
        for adapter in self._adapters.values():
            await adapter.close()
        self._adapters.clear()


# Singleton instance
_processor: SignalProcessor | None = None


def get_processor() -> SignalProcessor:
    """Get the signal processor singleton."""
    global _processor
    if _processor is None:
        _processor = SignalProcessor()
    return _processor


async def process_bravos_email(
    email_message_id: str,
    email_payload: dict[str, Any] | None = None,
) -> ProcessingResult:
    """
    Convenience function to process a Bravos email.

    Args:
        email_message_id: Gmail message ID
        email_payload: Optional email metadata

    Returns:
        ProcessingResult
    """
    processor = get_processor()
    return await processor.process_bravos_email(email_message_id, email_payload)
