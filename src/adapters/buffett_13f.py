"""
Buffett 13F adapter.

Fetches portfolio data from Berkshire Hathaway's SEC 13F-HR filings.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.adapters.base import (
    AdapterError,
    Allocation,
    PortfolioSnapshot,
    SleeveAdapter,
)
from src.signals.sec_edgar import (
    CIK_BERKSHIRE,
    Filing13F,
    SECEdgar13FFetcher,
    weight_bucket_to_integer,
)

logger = structlog.get_logger(__name__)

# Default configuration
DEFAULT_CONFIG = {
    "cik": CIK_BERKSHIRE,
    "top_n_positions": 10,
    "min_portfolio_weight_pct": 3.0,
    "exclude_cusips": [],
    "dollars_per_weight": 500,
}


class Buffett13FAdapter(SleeveAdapter):
    """
    Adapter for Berkshire Hathaway 13F-based portfolio.

    Fetches the latest 13F-HR filing from SEC EDGAR, filters to top
    high-conviction positions, and converts to the standard allocation
    format for reconciliation.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_path: Path | None = None,
    ):
        """
        Initialize the Buffett adapter.

        Args:
            config: Configuration dict. If not provided, loads from config_path.
            config_path: Path to JSON config file. Defaults to
                        data/config/buffett_sleeve.json
        """
        self._config_path = config_path or Path("data/config/buffett_sleeve.json")
        self._config = config or self._load_config()
        self._fetcher = SECEdgar13FFetcher()
        self._cached_filing: Filing13F | None = None

    def _load_config(self) -> dict[str, Any]:
        """Load configuration from JSON file."""
        if self._config_path.exists():
            with open(self._config_path) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        return DEFAULT_CONFIG

    @property
    def config(self) -> dict[str, Any]:
        """Get current configuration."""
        return self._config

    @property
    def sleeve_name(self) -> str:
        return "buffett"

    @property
    def adapter_type(self) -> str:
        return "sec_13f"

    async def fetch_portfolio(
        self, force_refresh: bool = False
    ) -> PortfolioSnapshot | AdapterError:
        """
        Fetch current Buffett portfolio from SEC 13F filing.

        Args:
            force_refresh: Force a fresh fetch from SEC EDGAR

        Returns:
            PortfolioSnapshot on success, AdapterError on failure
        """
        log = logger.bind(sleeve=self.sleeve_name, force_refresh=force_refresh)
        log.info("fetch_portfolio_started")

        try:
            # Fetch latest 13F
            filing = await self._fetcher.fetch_latest_13f(self._config["cik"])

            if filing is None:
                log.error("failed_to_fetch_13f")
                return AdapterError(
                    error_type="fetch",
                    message="Failed to fetch 13F filing from SEC EDGAR",
                    recoverable=True,
                )

            self._cached_filing = filing

            # Filter and convert to allocations
            allocations = self._convert_to_allocations(filing)

            snapshot = PortfolioSnapshot(
                sleeve_name=self.sleeve_name,
                allocations=allocations,
                scraped_at=datetime.now(timezone.utc),
                last_updated=datetime.combine(
                    filing.report_date, datetime.min.time()
                ).replace(tzinfo=timezone.utc),
                total_positions=len(allocations),
                raw_data={
                    "cik": filing.cik,
                    "filer_name": filing.filer_name,
                    "report_date": str(filing.report_date),
                    "filed_date": str(filing.filed_date),
                    "accession_number": filing.accession_number,
                    "total_positions_in_filing": filing.position_count,
                    "total_value": filing.total_value,
                },
            )

            log.info(
                "fetch_portfolio_completed",
                positions=snapshot.total_positions,
                report_date=str(filing.report_date),
                filed_date=str(filing.filed_date),
            )

            return snapshot

        except Exception as e:
            log.exception("fetch_portfolio_unexpected_error", error=str(e))
            return AdapterError(
                error_type="unknown",
                message=str(e),
                recoverable=False,
                raw_error=e,
            )

    def _convert_to_allocations(self, filing: Filing13F) -> list[Allocation]:
        """
        Convert 13F holdings to allocations.

        Filters to top positions by weight and assigns integer weights
        for fixed-dollar reconciliation.

        Args:
            filing: Parsed 13F filing

        Returns:
            List of Allocation objects
        """
        log = logger.bind(sleeve=self.sleeve_name)
        min_weight = self._config["min_portfolio_weight_pct"]
        top_n = self._config["top_n_positions"]
        exclude_cusips = set(self._config.get("exclude_cusips", []))

        allocations = []
        skipped_no_ticker = []
        skipped_low_weight = []
        skipped_excluded = []

        # Sort holdings by value (descending)
        sorted_holdings = sorted(filing.holdings, key=lambda h: h.value, reverse=True)

        for holding in sorted_holdings:
            # Skip excluded CUSIPs
            if holding.cusip in exclude_cusips:
                skipped_excluded.append(holding.issuer_name)
                continue

            # Skip if no ticker resolution
            if not holding.ticker:
                skipped_no_ticker.append(
                    f"{holding.issuer_name} ({holding.cusip})"
                )
                continue

            # Skip if below minimum weight
            weight_pct = holding.weight_pct or 0
            if weight_pct < min_weight:
                skipped_low_weight.append(
                    f"{holding.ticker} ({weight_pct:.1f}%)"
                )
                continue

            # Convert to integer weight
            int_weight = weight_bucket_to_integer(weight_pct)
            if int_weight == 0:
                skipped_low_weight.append(
                    f"{holding.ticker} ({weight_pct:.1f}%)"
                )
                continue

            # Calculate target_weight as proportion of total weight
            # (will be normalized later in reconciliation)
            allocations.append(
                Allocation(
                    symbol=holding.ticker,
                    target_weight=weight_pct / 100,  # Store as decimal
                    side="long",
                    raw_weight=int_weight,
                    asset_name=holding.issuer_name,
                    category=f"13F:{holding.cusip}",
                )
            )

            # Stop at top N
            if len(allocations) >= top_n:
                break

        # Log filtering results
        if skipped_no_ticker:
            log.debug(
                "skipped_no_ticker",
                count=len(skipped_no_ticker),
                holdings=skipped_no_ticker[:5],  # First 5 only
            )

        if skipped_low_weight:
            log.debug(
                "skipped_low_weight",
                count=len(skipped_low_weight),
                min_weight=min_weight,
            )

        if skipped_excluded:
            log.debug(
                "skipped_excluded",
                count=len(skipped_excluded),
            )

        log.info(
            "converted_allocations",
            included=len(allocations),
            total_in_filing=len(filing.holdings),
            total_weight_sum=sum(a.raw_weight or 0 for a in allocations),
        )

        return allocations

    async def check_health(self) -> dict[str, Any]:
        """
        Check if SEC EDGAR is reachable.

        Returns:
            Health status dict
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://data.sec.gov/submissions/CIK0000000000.json",
                    headers={"User-Agent": "HealthCheck/1.0"},
                )
                # We expect 404 for non-existent CIK, but that means EDGAR is up
                return {
                    "healthy": response.status_code in (200, 404),
                    "status": "reachable",
                    "edgar_status_code": response.status_code,
                }
        except Exception as e:
            logger.warning("health_check_failed", error=str(e))
            return {
                "healthy": False,
                "status": "unreachable",
                "error": str(e),
            }

    async def close(self):
        """Clean up resources."""
        pass

    def get_cached_filing(self) -> Filing13F | None:
        """
        Get the most recently fetched filing.

        Useful for debugging and inspection.
        """
        return self._cached_filing
