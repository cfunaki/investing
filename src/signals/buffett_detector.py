"""
Buffett sleeve 13F filing detector.

Detects new SEC 13F filings from Berkshire Hathaway and triggers
the processing pipeline when a new filing is found.

State tracking:
- Stores processed accession numbers in the database signals table
- Compares against latest filing from SEC EDGAR
- Triggers processing only when new filing is detected
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from src.db.repositories.signal_repository import signal_repository
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context
from src.signals.sec_edgar import SECEdgar13FFetcher, Filing13F

logger = structlog.get_logger(__name__)

# Buffett sleeve name
BUFFETT_SLEEVE_NAME = "buffett"


@dataclass
class FilingDetectionResult:
    """Result from checking for new 13F filings."""

    new_filing_detected: bool
    filing: Filing13F | None = None
    previous_accession: str | None = None
    current_accession: str | None = None
    error: str | None = None


class Buffett13FDetector:
    """
    Detects new Berkshire Hathaway 13F filings.

    Maintains state in database to know which filings have already been processed.
    """

    def __init__(
        self,
        cik: str = "0001067983",  # Berkshire Hathaway
    ):
        self.cik = cik
        self.fetcher = SECEdgar13FFetcher()
        self._sleeve_id: UUID | None = None

    async def _get_sleeve_id(self) -> UUID | None:
        """Get the Buffett sleeve ID from database."""
        if self._sleeve_id:
            return self._sleeve_id

        try:
            async with get_db_context() as db:
                sleeve = await sleeve_repository.get_by_name(db, BUFFETT_SLEEVE_NAME)
                if sleeve:
                    self._sleeve_id = sleeve.id
                    return self._sleeve_id
        except Exception as e:
            logger.warning("failed_to_get_sleeve_id", error=str(e))

        return None

    async def get_last_processed_accession(self) -> str | None:
        """Get the accession number of the last processed filing."""
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

    async def mark_as_processed(
        self,
        accession_number: str,
        report_date: datetime | None = None,
        details: dict[str, Any] | None = None,
    ):
        """
        Mark a filing as processed by creating a signal record.

        Args:
            accession_number: The accession number of the processed filing
            report_date: The report period date
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
                    db, sleeve_id, accession_number
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
                        source_event_id=accession_number,
                        event_type="13f_filing",
                        detected_at=datetime.now(timezone.utc),
                        raw_payload={
                            "report_date": report_date.isoformat() if report_date else None,
                            "details": details,
                        },
                        status="processed",
                    )

                logger.info(
                    "marked_filing_as_processed",
                    accession_number=accession_number,
                    report_date=str(report_date) if report_date else None,
                )

        except Exception as e:
            logger.exception("failed_to_mark_as_processed", error=str(e))

    async def check_for_new_filing(self) -> FilingDetectionResult:
        """
        Check SEC EDGAR for a new Berkshire 13F filing.

        Returns:
            FilingDetectionResult indicating if a new filing was found
        """
        log = logger.bind(cik=self.cik)
        log.info("checking_for_new_13f_filing")

        try:
            # Fetch latest filing from SEC EDGAR
            filing = await self.fetcher.fetch_latest_13f(self.cik)

            if filing is None:
                log.warning("no_13f_filing_found")
                return FilingDetectionResult(
                    new_filing_detected=False,
                    error="No 13F filing found for CIK",
                )

            current_accession = filing.accession_number
            previous_accession = await self.get_last_processed_accession()

            log = log.bind(
                current_accession=current_accession,
                previous_accession=previous_accession,
                report_date=str(filing.report_date),
            )

            # Check if this is a new filing
            if previous_accession is None:
                # First time checking - this is "new" by default
                log.info("first_filing_detected")
                return FilingDetectionResult(
                    new_filing_detected=True,
                    filing=filing,
                    previous_accession=None,
                    current_accession=current_accession,
                )

            if current_accession != previous_accession:
                log.info("new_filing_detected")
                return FilingDetectionResult(
                    new_filing_detected=True,
                    filing=filing,
                    previous_accession=previous_accession,
                    current_accession=current_accession,
                )

            log.info("no_new_filing")
            return FilingDetectionResult(
                new_filing_detected=False,
                filing=filing,
                previous_accession=previous_accession,
                current_accession=current_accession,
            )

        except Exception as e:
            log.exception("filing_check_failed", error=str(e))
            return FilingDetectionResult(
                new_filing_detected=False,
                error=str(e),
            )

    async def get_status(self) -> dict[str, Any]:
        """Get the current detector status from database."""
        sleeve_id = await self._get_sleeve_id()
        if not sleeve_id:
            return {
                "cik": self.cik,
                "error": "sleeve_not_found",
                "last_processed_accession": None,
            }

        try:
            async with get_db_context() as db:
                # Get last processed signal
                last_signal = await signal_repository.get_last_processed(db, sleeve_id)

                # Get recent signals count
                recent = await signal_repository.get_recent(db, sleeve_id, limit=10)

                return {
                    "cik": self.cik,
                    "sleeve_id": str(sleeve_id),
                    "last_processed_accession": last_signal.source_event_id if last_signal else None,
                    "last_processed_at": last_signal.processed_at.isoformat() if last_signal and last_signal.processed_at else None,
                    "history_count": len(recent),
                }
        except Exception as e:
            logger.warning("failed_to_get_status", error=str(e))
            return {"cik": self.cik, "error": str(e)}


# Singleton instance
_detector: Buffett13FDetector | None = None


def get_buffett_detector() -> Buffett13FDetector:
    """Get the Buffett 13F detector singleton."""
    global _detector
    if _detector is None:
        _detector = Buffett13FDetector()
    return _detector
