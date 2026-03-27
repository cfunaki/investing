"""
Buffett sleeve 13F filing detector.

Detects new SEC 13F filings from Berkshire Hathaway and triggers
the processing pipeline when a new filing is found.

State tracking:
- Stores last processed accession number in a JSON file
- Compares against latest filing from SEC EDGAR
- Triggers processing only when new filing is detected
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.signals.sec_edgar import SECEdgar13FFetcher, Filing13F

logger = structlog.get_logger(__name__)

# Default state file location
STATE_FILE = Path("data/state/buffett_13f_state.json")


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

    Maintains state to know which filings have already been processed.
    """

    def __init__(
        self,
        state_file: Path = STATE_FILE,
        cik: str = "0001067983",  # Berkshire Hathaway
    ):
        self.state_file = state_file
        self.cik = cik
        self.fetcher = SECEdgar13FFetcher()

        # Ensure state directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict[str, Any]:
        """Load the current state from disk."""
        if not self.state_file.exists():
            return {
                "last_processed_accession": None,
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

    def get_last_processed_accession(self) -> str | None:
        """Get the accession number of the last processed filing."""
        state = self._load_state()
        return state.get("last_processed_accession")

    def mark_as_processed(
        self,
        accession_number: str,
        report_date: datetime | None = None,
        details: dict[str, Any] | None = None,
    ):
        """
        Mark a filing as processed.

        Args:
            accession_number: The accession number of the processed filing
            report_date: The report period date
            details: Optional processing details to record
        """
        state = self._load_state()

        now = datetime.now(timezone.utc).isoformat()

        state["last_processed_accession"] = accession_number
        state["last_processed_at"] = now

        # Add to history
        history_entry = {
            "accession_number": accession_number,
            "report_date": report_date.isoformat() if report_date else None,
            "processed_at": now,
        }
        if details:
            history_entry["details"] = details

        state.setdefault("processing_history", []).append(history_entry)

        # Keep only last 10 entries
        state["processing_history"] = state["processing_history"][-10:]

        self._save_state(state)

        logger.info(
            "marked_filing_as_processed",
            accession_number=accession_number,
            report_date=str(report_date) if report_date else None,
        )

    async def check_for_new_filing(self) -> FilingDetectionResult:
        """
        Check SEC EDGAR for a new Berkshire 13F filing.

        Returns:
            FilingDetectionResult indicating if a new filing was found
        """
        log = logger.bind(cik=self.cik)
        log.info("checking_for_new_13f_filing")

        # Update last checked timestamp
        state = self._load_state()
        state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)

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
            previous_accession = self.get_last_processed_accession()

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

    def get_status(self) -> dict[str, Any]:
        """Get the current detector status."""
        state = self._load_state()
        return {
            "cik": self.cik,
            "last_processed_accession": state.get("last_processed_accession"),
            "last_checked_at": state.get("last_checked_at"),
            "last_processed_at": state.get("last_processed_at"),
            "history_count": len(state.get("processing_history", [])),
        }


# Singleton instance
_detector: Buffett13FDetector | None = None


def get_buffett_detector() -> Buffett13FDetector:
    """Get the Buffett 13F detector singleton."""
    global _detector
    if _detector is None:
        _detector = Buffett13FDetector()
    return _detector
