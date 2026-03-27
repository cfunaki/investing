"""
Bravos email detector with state tracking.

Detects new portfolio update emails from Bravos Research and triggers
the processing pipeline when a new email is found.

State tracking:
- Stores last processed email message ID in a JSON file
- Compares against latest emails from Gmail
- Triggers processing only when new email is detected
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.signals.email_monitor import GmailMonitor, get_email_monitor, EmailMessage

logger = structlog.get_logger(__name__)

# Default state file location
STATE_FILE = Path("data/state/bravos_email_state.json")


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

    Maintains state to know which emails have already been processed.
    """

    def __init__(
        self,
        state_file: Path = STATE_FILE,
        monitor: GmailMonitor | None = None,
    ):
        self.state_file = state_file
        self.monitor = monitor

        # Ensure state directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def _get_monitor(self) -> GmailMonitor:
        """Get or create the Gmail monitor."""
        if self.monitor is None:
            self.monitor = get_email_monitor()
        return self.monitor

    def _load_state(self) -> dict[str, Any]:
        """Load the current state from disk."""
        if not self.state_file.exists():
            return {
                "last_processed_message_id": None,
                "last_checked_at": None,
                "last_processed_at": None,
                "processing_history": [],
            }

        with open(self.state_file) as f:
            return json.load(f)

    def _save_state(self, state: dict[str, Any]):
        """Save state to disk."""
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def get_last_processed_message_id(self) -> str | None:
        """Get the message ID of the last processed email."""
        state = self._load_state()
        return state.get("last_processed_message_id")

    def get_processed_message_ids(self) -> set[str]:
        """Get all processed message IDs from history."""
        state = self._load_state()
        ids = set()
        if state.get("last_processed_message_id"):
            ids.add(state["last_processed_message_id"])
        for entry in state.get("processing_history", []):
            if entry.get("message_id"):
                ids.add(entry["message_id"])
        return ids

    def mark_as_processed(
        self,
        message_id: str,
        subject: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        """
        Mark an email as processed.

        Args:
            message_id: The Gmail message ID of the processed email
            subject: The email subject
            details: Optional processing details to record
        """
        state = self._load_state()

        now = datetime.now(timezone.utc).isoformat()

        state["last_processed_message_id"] = message_id
        state["last_processed_at"] = now

        # Add to history
        history_entry = {
            "message_id": message_id,
            "subject": subject,
            "processed_at": now,
        }
        if details:
            history_entry["details"] = details

        state.setdefault("processing_history", []).append(history_entry)

        # Keep only last 20 entries
        state["processing_history"] = state["processing_history"][-20:]

        self._save_state(state)

        logger.info(
            "marked_email_as_processed",
            message_id=message_id,
            subject=subject,
        )

    async def check_for_new_email(self) -> EmailDetectionResult:
        """
        Check Gmail for a new Bravos portfolio update email.

        Returns:
            EmailDetectionResult indicating if a new email was found
        """
        log = logger.bind()
        log.info("checking_for_new_bravos_email")

        # Update last checked timestamp
        state = self._load_state()
        state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)

        try:
            # Get processed message IDs to skip
            processed_ids = self.get_processed_message_ids()

            # Check for new emails
            monitor = self._get_monitor()
            emails = await monitor.check_for_emails(
                max_results=5,
                processed_ids=processed_ids,
            )

            previous_message_id = self.get_last_processed_message_id()

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

    def get_status(self) -> dict[str, Any]:
        """Get the current detector status."""
        state = self._load_state()
        return {
            "last_processed_message_id": state.get("last_processed_message_id"),
            "last_checked_at": state.get("last_checked_at"),
            "last_processed_at": state.get("last_processed_at"),
            "history_count": len(state.get("processing_history", [])),
        }


# Singleton instance
_detector: BravosEmailDetector | None = None


def get_bravos_detector() -> BravosEmailDetector:
    """Get the Bravos email detector singleton."""
    global _detector
    if _detector is None:
        _detector = BravosEmailDetector()
    return _detector
