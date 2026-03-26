"""
Bravos Research web adapter.

Fetches portfolio data from Bravos Research by calling the browser-worker service.
The browser-worker handles the actual Playwright scraping.
"""

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from src.adapters.base import (
    AdapterError,
    Allocation,
    PortfolioSnapshot,
    SleeveAdapter,
)
from src.adapters.http_client import BrowserWorkerClient, get_browser_worker_client

logger = structlog.get_logger(__name__)


class BravosWebAdapter(SleeveAdapter):
    """
    Adapter for fetching Bravos Research portfolio data.

    This adapter delegates to the browser-worker service which handles
    the actual Playwright-based web scraping.

    The separation allows:
    - Independent scaling of browser automation
    - Isolation of browser-related failures
    - Cleaner orchestrator code
    """

    def __init__(self, client: BrowserWorkerClient | None = None):
        """
        Initialize the Bravos adapter.

        Args:
            client: Optional HTTP client for browser worker.
                   If not provided, uses the singleton client.
        """
        self._client = client

    @property
    def client(self) -> BrowserWorkerClient:
        """Get the browser worker client."""
        if self._client is None:
            self._client = get_browser_worker_client()
        return self._client

    @property
    def sleeve_name(self) -> str:
        return "bravos"

    @property
    def adapter_type(self) -> str:
        return "bravos_web"

    async def fetch_portfolio(
        self, force_refresh: bool = False
    ) -> PortfolioSnapshot | AdapterError:
        """
        Fetch current Bravos portfolio by calling browser-worker.

        Args:
            force_refresh: Force a fresh scrape

        Returns:
            PortfolioSnapshot on success, AdapterError on failure
        """
        log = logger.bind(sleeve=self.sleeve_name, force_refresh=force_refresh)
        log.info("fetch_portfolio_started")

        try:
            # Call browser worker
            response = await self.client.scrape_bravos(force_refresh=force_refresh)

            # Check for scraper-level errors
            if not response.get("success", False):
                error_type = response.get("error_type", "unknown")
                error_msg = response.get("error", "Unknown error")

                log.warning(
                    "fetch_portfolio_failed",
                    error_type=error_type,
                    error=error_msg,
                    latency_ms=response.get("latency_ms"),
                )

                return AdapterError(
                    error_type=error_type,
                    message=error_msg,
                    recoverable=error_type in ("timeout", "network"),
                )

            # Parse allocations from response
            allocations = self._parse_allocations(response.get("allocations", []))

            # Parse timestamps
            scraped_at = datetime.fromisoformat(
                response["scraped_at"].replace("Z", "+00:00")
            )

            last_updated = None
            if response.get("last_updated"):
                # Try to parse the last_updated string (format varies)
                try:
                    last_updated = self._parse_last_updated(response["last_updated"])
                except Exception as e:
                    log.warning("failed_to_parse_last_updated", error=str(e))

            snapshot = PortfolioSnapshot(
                sleeve_name=self.sleeve_name,
                allocations=allocations,
                scraped_at=scraped_at,
                last_updated=last_updated,
                total_positions=response.get("total_positions", len(allocations)),
                raw_data=response,
                latency_ms=response.get("latency_ms", 0),
                cold_start=response.get("cold_start", False),
            )

            log.info(
                "fetch_portfolio_completed",
                positions=snapshot.total_positions,
                latency_ms=snapshot.latency_ms,
                cold_start=snapshot.cold_start,
            )

            return snapshot

        except httpx.TimeoutException as e:
            log.error("fetch_portfolio_timeout", error=str(e))
            return AdapterError(
                error_type="timeout",
                message=f"Browser worker request timed out: {e}",
                recoverable=True,
                raw_error=e,
            )

        except httpx.RequestError as e:
            log.error("fetch_portfolio_network_error", error=str(e))
            return AdapterError(
                error_type="network",
                message=f"Failed to reach browser worker: {e}",
                recoverable=True,
                raw_error=e,
            )

        except Exception as e:
            log.exception("fetch_portfolio_unexpected_error", error=str(e))
            return AdapterError(
                error_type="unknown",
                message=str(e),
                recoverable=False,
                raw_error=e,
            )

    def _parse_allocations(self, raw_allocations: list[dict[str, Any]]) -> list[Allocation]:
        """
        Parse raw allocation data from browser worker response.
        """
        allocations = []

        for raw in raw_allocations:
            try:
                allocations.append(
                    Allocation(
                        symbol=raw["symbol"],
                        target_weight=raw["target_weight"],
                        side=raw.get("side", "long"),
                        raw_weight=raw.get("raw_weight"),
                        asset_name=raw.get("asset_name"),
                    )
                )
            except (KeyError, TypeError) as e:
                logger.warning(
                    "failed_to_parse_allocation",
                    raw=raw,
                    error=str(e),
                )

        return allocations

    def _parse_last_updated(self, last_updated_str: str) -> datetime:
        """
        Parse the last_updated string from Bravos.

        The format is typically like "December 15, 2024" or similar.
        """
        from dateutil import parser

        # Use dateutil for flexible parsing
        return parser.parse(last_updated_str)

    async def check_health(self) -> dict[str, Any]:
        """
        Check if the browser worker is healthy.

        Returns:
            Health status dict
        """
        try:
            response = await self.client.health_check()
            return {
                "healthy": response.get("status") in ("healthy", "needs_auth"),
                "status": response.get("status"),
                "node_available": response.get("node_available"),
                "session_exists": response.get("session_exists"),
                "latency_info": response.get("latency_info"),
            }
        except Exception as e:
            logger.warning("health_check_failed", error=str(e))
            return {
                "healthy": False,
                "status": "unreachable",
                "error": str(e),
            }

    async def close(self):
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.close()
