"""
Gmail email monitor for detecting Bravos portfolio update notifications.

This module polls Gmail for new emails from Bravos and triggers
signal processing for each new email detected.

The email is a TRIGGER only - we don't parse trade data from the email.
Instead, detecting an email triggers a scrape of the actual Bravos website
to get the current portfolio state.
"""

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src.config import get_settings

logger = structlog.get_logger(__name__)

# Gmail API scopes - read-only access to emails
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Search query for Bravos emails
BRAVOS_EMAIL_QUERY = 'from:bravosresearch.com subject:"portfolio" OR subject:"trade" OR subject:"update"'


@dataclass
class EmailMessage:
    """A detected email message."""

    message_id: str
    thread_id: str
    subject: str
    sender: str
    received_at: datetime
    snippet: str
    labels: list[str]

    def to_payload(self) -> dict[str, Any]:
        """Convert to payload dict for signal processing."""
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "sender": self.sender,
            "received_at": self.received_at.isoformat(),
            "snippet": self.snippet,
            "labels": self.labels,
        }


class GmailMonitor:
    """
    Monitors Gmail for Bravos portfolio update emails.

    This is a polling-based monitor that:
    1. Connects to Gmail API using OAuth2
    2. Searches for recent Bravos emails
    3. Returns new emails that haven't been processed

    The caller is responsible for tracking which emails have been processed
    (via the signals table in the database).
    """

    def __init__(
        self,
        credentials_json: str | None = None,
        credentials_path: str | None = None,
        token_path: str | None = None,
        token_json: str | None = None,
    ):
        """
        Initialize the Gmail monitor.

        Args:
            credentials_json: OAuth credentials as JSON string (for Cloud Run)
            credentials_path: Path to OAuth credentials file (for local dev)
            token_path: Path to store OAuth token after authentication
            token_json: Pre-existing OAuth token as JSON string (for Cloud Run via Secret Manager)
        """
        settings = get_settings()

        self.credentials_json = credentials_json or settings.gmail_credentials_json
        self.credentials_path = credentials_path or settings.gmail_credentials_path
        self.token_path = token_path or settings.gmail_token_path
        self.token_json = token_json or settings.gmail_token_json

        self._service = None
        self._credentials = None

    def _get_credentials(self) -> Credentials:
        """Get or refresh OAuth2 credentials."""
        creds = None
        token_path = Path(self.token_path)

        # Priority 1: Check for token as JSON string (Cloud Run via Secret Manager)
        if self.token_json:
            try:
                token_data = json.loads(self.token_json)
                creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                logger.info("loaded_gmail_token_from_json")
            except Exception as e:
                logger.warning("failed_to_load_token_from_json", error=str(e))

        # Priority 2: Check for existing token file (local dev)
        if not creds and token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                logger.info("loaded_gmail_token_from_file", path=str(token_path))
            except Exception as e:
                logger.warning("failed_to_load_token_from_file", error=str(e))

        # Priority 3: Check database backup (key_value_state)
        if not creds:
            try:
                import asyncio
                from src.db.repositories.state_repository import state_repository
                from src.db.session import get_db_context

                async def _load():
                    async with get_db_context() as db:
                        return await state_repository.get_state(db, "gmail_token")

                try:
                    loop = asyncio.get_running_loop()
                    future = asyncio.run_coroutine_threadsafe(_load(), loop)
                    state = future.result(timeout=10)
                except RuntimeError:
                    state = asyncio.run(_load())

                if state and state.get("token_json"):
                    token_data = json.loads(state["token_json"])
                    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                    logger.info("loaded_gmail_token_from_database")
            except Exception as e:
                logger.warning("failed_to_load_token_from_database", error=str(e))

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("refreshed_gmail_credentials")

                # Persist refreshed token to Secret Manager + database
                self._persist_refreshed_token(creds)
            except Exception as e:
                logger.warning("failed_to_refresh_credentials", error=str(e))
                creds = None

        if not creds or not creds.valid:
            # Need to authenticate - this requires interactive OAuth flow
            # In Cloud Run, this should not happen if GMAIL_TOKEN_JSON is properly set
            if self.credentials_json:
                # Parse credentials from JSON string (Cloud Run)
                creds_data = json.loads(self.credentials_json)
                flow = InstalledAppFlow.from_client_config(creds_data, SCOPES)
            elif self.credentials_path:
                # Load credentials from file (local dev)
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
            else:
                raise ValueError(
                    "No Gmail credentials configured. Set GMAIL_TOKEN_JSON (preferred), "
                    "GMAIL_CREDENTIALS_JSON, or GMAIL_CREDENTIALS_PATH"
                )

            # Run local server for OAuth flow - only works in interactive mode
            logger.warning("running_interactive_oauth_flow")
            creds = flow.run_local_server(port=0)

            # Save the token for future use
            token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "w") as token_file:
                token_file.write(creds.to_json())

            logger.info("saved_new_gmail_token", path=str(token_path))

        return creds

    def _persist_refreshed_token(self, creds) -> None:
        """Write refreshed Gmail token to Secret Manager and database."""
        token_json = creds.to_json()

        # 1. Try Secret Manager
        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "investing-automation-490206")
            secret_path = f"projects/{project_id}/secrets/GMAIL_TOKEN_JSON"
            client.add_secret_version(
                request={
                    "parent": secret_path,
                    "payload": {"data": token_json.encode("utf-8")},
                }
            )
            logger.info("gmail_token_persisted_to_secret_manager")
        except Exception as e:
            logger.warning("gmail_token_secret_manager_write_failed", error=str(e))

        # 2. Backup to database key_value_state
        try:
            import asyncio
            from src.db.repositories.state_repository import state_repository
            from src.db.session import get_db_context

            async def _save():
                async with get_db_context() as db:
                    await state_repository.set_state(db, "gmail_token", {
                        "token_json": token_json,
                        "refreshed_at": datetime.now(timezone.utc).isoformat(),
                    })

            try:
                loop = asyncio.get_running_loop()
                future = asyncio.run_coroutine_threadsafe(_save(), loop)
                future.result(timeout=10)
            except RuntimeError:
                asyncio.run(_save())
            logger.info("gmail_token_persisted_to_database")
        except Exception as e:
            logger.warning("gmail_token_database_write_failed", error=str(e))

    def _get_service(self):
        """Get Gmail API service (lazy initialization)."""
        if self._service is None:
            self._credentials = self._get_credentials()
            self._service = build("gmail", "v1", credentials=self._credentials)
        return self._service

    async def check_for_emails(
        self,
        query: str = BRAVOS_EMAIL_QUERY,
        max_results: int = 10,
        processed_ids: set[str] | None = None,
    ) -> list[EmailMessage]:
        """
        Check for new Bravos emails.

        Args:
            query: Gmail search query
            max_results: Maximum emails to return
            processed_ids: Set of already-processed message IDs to skip

        Returns:
            List of new EmailMessage objects
        """
        # Force fresh connection to avoid stale socket errors
        self._service = None
        log = logger.bind(query=query, max_results=max_results)
        log.info("checking_for_emails")

        processed_ids = processed_ids or set()

        try:
            service = self._get_service()

            # Search for matching messages
            results = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )

            messages = results.get("messages", [])

            if not messages:
                log.info("no_emails_found")
                return []

            log.info("found_email_candidates", count=len(messages))

            # Filter out already processed messages
            new_messages = []
            for msg_ref in messages:
                msg_id = msg_ref["id"]

                if msg_id in processed_ids:
                    continue

                # Fetch full message details
                email = self._fetch_message(service, msg_id)
                if email:
                    new_messages.append(email)

            log.info(
                "new_emails_found",
                total=len(messages),
                new=len(new_messages),
                skipped=len(messages) - len(new_messages),
            )

            return new_messages

        except Exception as e:
            log.exception("email_check_failed", error=str(e))
            raise

    def _fetch_message(self, service, message_id: str) -> EmailMessage | None:
        """Fetch full message details."""
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata")
                .execute()
            )

            # Extract headers
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

            # Parse date
            internal_date = msg.get("internalDate")
            if internal_date:
                received_at = datetime.fromtimestamp(
                    int(internal_date) / 1000, tz=timezone.utc
                )
            else:
                received_at = datetime.now(timezone.utc)

            return EmailMessage(
                message_id=message_id,
                thread_id=msg.get("threadId", ""),
                subject=headers.get("subject", "(no subject)"),
                sender=headers.get("from", ""),
                received_at=received_at,
                snippet=msg.get("snippet", ""),
                labels=msg.get("labelIds", []),
            )

        except Exception as e:
            logger.warning(
                "failed_to_fetch_message", message_id=message_id, error=str(e)
            )
            return None

    async def poll_and_process(
        self,
        processed_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Poll for new emails and prepare them for signal processing.

        This is a convenience method that:
        1. Checks for new Bravos emails
        2. Returns payloads ready for signal processor

        The caller should:
        1. For each email, call process_bravos_email(email.message_id, payload)
        2. Track processed message IDs to avoid reprocessing

        Args:
            processed_ids: Set of already-processed message IDs

        Returns:
            List of payloads for signal processing
        """
        emails = await self.check_for_emails(processed_ids=processed_ids)

        results = []
        for email in emails:
            results.append({
                "message_id": email.message_id,
                "payload": email.to_payload(),
            })

            logger.info(
                "email_ready_for_processing",
                message_id=email.message_id,
                subject=email.subject,
                received_at=email.received_at.isoformat(),
            )

        return results


# Singleton instance
_monitor: GmailMonitor | None = None


def get_email_monitor() -> GmailMonitor:
    """Get the Gmail monitor singleton."""
    global _monitor
    if _monitor is None:
        _monitor = GmailMonitor()
    return _monitor


async def poll_for_bravos_emails(
    processed_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Convenience function to poll for Bravos emails.

    Args:
        processed_ids: Set of already-processed message IDs to skip

    Returns:
        List of email payloads ready for signal processing
    """
    monitor = get_email_monitor()
    return await monitor.poll_and_process(processed_ids=processed_ids)
