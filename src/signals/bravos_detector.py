"""
Bravos email detector with state tracking.

Detects new portfolio update emails from Bravos Research and triggers
the processing pipeline when a new email is found.

State tracking:
- Stores processed email message IDs in the database signals table
- Compares against latest emails from Gmail
- Triggers processing only when new email is detected
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from src.db.repositories.signal_repository import signal_repository
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context
from src.signals.email_monitor import GmailMonitor, get_email_monitor, EmailMessage

logger = structlog.get_logger(__name__)

# Bravos sleeve name
BRAVOS_SLEEVE_NAME = "bravos"


@dataclass
class EmailDetectionResult:
    """Result from checking for new Bravos emails."""

    new_email_detected: bool
    email: EmailMessage | None = None
    previous_message_id: str | None = None
    current_message_id: str | None = None
    error: str | None = None


class BravosEmailDetector:
    """
    Detects new Bravos portfolio update emails.

    Maintains state in database to know which emails have already been processed.
    """

    def __init__(
        self,
        monitor: GmailMonitor | None = None,
    ):
        self.monitor = monitor
        self._sleeve_id: UUID | None = None

    def _get_monitor(self) -> GmailMonitor:
        """Get or create the Gmail monitor."""
        if self.monitor is None:
            self.monitor = get_email_monitor()
        return self.monitor

    async def _get_sleeve_id(self) -> UUID | None:
        """Get the Bravos sleeve ID from database."""
        if self._sleeve_id:
            return self._sleeve_id

        try:
            async with get_db_context() as db:
                sleeve = await sleeve_repository.get_by_name(db, BRAVOS_SLEEVE_NAME)
                if sleeve:
                    self._sleeve_id = sleeve.id
                    return self._sleeve_id
        except Exception as e:
            logger.warning("failed_to_get_sleeve_id", error=str(e))

        return None

    async def get_last_processed_message_id(self) -> str | None:
        """Get the message ID of the last processed email."""
        sleeve_id = await self._get_sleeve_id()
        if not sleeve_id:
            return None

        try:
            async with get_db_context() as db:
                signal = await signal_repository.get_last_processed(db, sleeve_id)
                if signal:
                    return signal.source_event_id
        except Exception as e:
            logger.warning("failed_to_get_last_processed", error=str(e))

        return None

    async def get_processed_message_ids(self) -> set[str]:
        """Get all processed message IDs from database."""
        sleeve_id = await self._get_sleeve_id()
        if not sleeve_id:
            return set()

        try:
            async with get_db_context() as db:
                return await signal_repository.get_processed_event_ids(db, sleeve_id)
        except Exception as e:
            logger.warning("failed_to_get_processed_ids", error=str(e))
            return set()

    async def mark_as_processed(
        self,
        message_id: str,
        subject: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        """
        Mark an email as processed by creating a signal record.

        Args:
            message_id: The Gmail message ID of the processed email
            subject: The email subject
            details: Optional processing details to record
        """
        sleeve_id = await self._get_sleeve_id()
        if not sleeve_id:
            logger.error("cannot_mark_processed_no_sleeve_id")
            return

        try:
            async with get_db_context() as db:
                # Check if already exists
                existing = await signal_repository.get_by_source_event_id(
                    db, sleeve_id, message_id
                )
                if existing:
                    # Update status to processed
                    await signal_repository.update_status(
                        db,
                        existing.id,
                        status="processed",
                        processed_at=datetime.now(timezone.utc),
                    )
                else:
                    # Create new signal record
                    await signal_repository.create(
                        db=db,
                        sleeve_id=sleeve_id,
                        source_event_id=message_id,
                        event_type="email_detected",
                        detected_at=datetime.now(timezone.utc),
                        raw_payload={
                            "subject": subject,
                            "details": details,
                        },
                        status="processed",
                    )

                logger.info(
                    "marked_email_as_processed",
                    message_id=message_id,
                    subject=subject,
                )

        except Exception as e:
            logger.exception("failed_to_mark_as_processed", error=str(e))

    async def check_for_new_email(self) -> EmailDetectionResult:
        """
        Check Gmail for a new Bravos portfolio update email.

        Returns:
            EmailDetectionResult indicating if a new email was found
        """
        log = logger.bind()
        log.info("checking_for_new_bravos_email")

        try:
            # Get processed message IDs from database
            processed_ids = await self.get_processed_message_ids()

            # Check for new emails
            monitor = self._get_monitor()
            emails = await monitor.check_for_emails(
                max_results=5,
                processed_ids=processed_ids,
            )

            previous_message_id = await self.get_last_processed_message_id()

            if not emails:
                log.info("no_new_bravos_emails")
                return EmailDetectionResult(
                    new_email_detected=False,
                    previous_message_id=previous_message_id,
                )

            # Take the most recent new email
            latest_email = emails[0]

            log = log.bind(
                message_id=latest_email.message_id,
                subject=latest_email.subject,
                received_at=latest_email.received_at.isoformat(),
            )

            log.info("new_bravos_email_detected")

            return EmailDetectionResult(
                new_email_detected=True,
                email=latest_email,
                previous_message_id=previous_message_id,
                current_message_id=latest_email.message_id,
            )

        except Exception as e:
            log.exception("email_check_failed", error=str(e))
            return EmailDetectionResult(
                new_email_detected=False,
                error=str(e),
            )

    async def get_status(self) -> dict[str, Any]:
        """Get the current detector status from database."""
        sleeve_id = await self._get_sleeve_id()
        if not sleeve_id:
            return {
                "error": "sleeve_not_found",
                "last_processed_message_id": None,
            }

        try:
            async with get_db_context() as db:
                # Get last processed signal
                last_signal = await signal_repository.get_last_processed(db, sleeve_id)

                # Get recent signals count
                recent = await signal_repository.get_recent(db, sleeve_id, limit=20)

                return {
                    "sleeve_id": str(sleeve_id),
                    "last_processed_message_id": last_signal.source_event_id if last_signal else None,
                    "last_processed_at": last_signal.processed_at.isoformat() if last_signal and last_signal.processed_at else None,
                    "history_count": len(recent),
                }
        except Exception as e:
            logger.warning("failed_to_get_status", error=str(e))
            return {"error": str(e)}


# Singleton instance
_detector: BravosEmailDetector | None = None


def get_bravos_detector() -> BravosEmailDetector:
    """Get the Bravos email detector singleton."""
    global _detector
    if _detector is None:
        _detector = BravosEmailDetector()
    return _detector
