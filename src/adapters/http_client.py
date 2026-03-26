"""
HTTP client utilities for service-to-service communication.

Provides a configured httpx client with:
- Automatic retries with exponential backoff
- Timeout configuration
- Structured logging
- Error handling
"""

import asyncio
from typing import Any

import httpx
import structlog

from src.config import get_settings

logger = structlog.get_logger(__name__)


class ServiceClient:
    """
    HTTP client for calling internal services (like browser-worker).

    Features:
    - Configurable timeout
    - Automatic retries with exponential backoff
    - Structured logging of requests/responses
    - Proper error handling
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 120.0,
        max_retries: int = 2,
        retry_delay: float = 1.0,
    ):
        """
        Initialize the service client.

        Args:
            base_url: Base URL for the service
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            retry_delay: Initial delay between retries (exponential backoff)
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Make a GET request with retries.

        Args:
            path: URL path (will be joined with base_url)
            params: Query parameters
            **kwargs: Additional arguments to pass to httpx

        Returns:
            httpx.Response

        Raises:
            httpx.HTTPError: If all retries fail
        """
        return await self._request("GET", path, params=params, **kwargs)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Make a POST request with retries.

        Args:
            path: URL path (will be joined with base_url)
            json: JSON body
            **kwargs: Additional arguments to pass to httpx

        Returns:
            httpx.Response

        Raises:
            httpx.HTTPError: If all retries fail
        """
        return await self._request("POST", path, json=json, **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        """
        Make an HTTP request with retries and logging.
        """
        client = await self._get_client()
        url = path.lstrip("/")

        log = logger.bind(
            method=method,
            url=f"{self.base_url}/{url}",
        )

        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                log.debug(
                    "http_request_started",
                    attempt=attempt + 1,
                    max_attempts=self.max_retries + 1,
                )

                response = await client.request(method, url, **kwargs)

                log.info(
                    "http_request_completed",
                    status_code=response.status_code,
                    attempt=attempt + 1,
                )

                # Don't retry on client errors (4xx)
                if response.status_code >= 400 and response.status_code < 500:
                    return response

                # Raise for server errors to trigger retry
                response.raise_for_status()
                return response

            except httpx.TimeoutException as e:
                last_error = e
                log.warning(
                    "http_request_timeout",
                    attempt=attempt + 1,
                    error=str(e),
                )

            except httpx.HTTPStatusError as e:
                last_error = e
                log.warning(
                    "http_request_error",
                    attempt=attempt + 1,
                    status_code=e.response.status_code,
                    error=str(e),
                )

            except httpx.RequestError as e:
                last_error = e
                log.warning(
                    "http_request_failed",
                    attempt=attempt + 1,
                    error=str(e),
                )

            # Wait before retrying (exponential backoff)
            if attempt < self.max_retries:
                delay = self.retry_delay * (2**attempt)
                log.debug("http_request_retry_wait", delay=delay)
                await asyncio.sleep(delay)

        # All retries exhausted
        log.error(
            "http_request_all_retries_failed",
            attempts=self.max_retries + 1,
            error=str(last_error),
        )

        if last_error is not None:
            raise last_error

        raise httpx.RequestError(f"Request to {url} failed after {self.max_retries + 1} attempts")


class BrowserWorkerClient(ServiceClient):
    """
    Client for communicating with the browser-worker service.

    Provides typed methods for browser worker endpoints.
    """

    def __init__(self):
        """Initialize with settings from config."""
        settings = get_settings()
        super().__init__(
            base_url=settings.browser_worker_url,
            timeout=settings.browser_worker_timeout,
        )

    async def health_check(self) -> dict[str, Any]:
        """
        Check browser worker health.

        Returns:
            Health check response dict
        """
        response = await self.get("/health")
        return response.json()

    async def scrape_bravos(self, force_refresh: bool = False) -> dict[str, Any]:
        """
        Scrape Bravos portfolio data.

        Args:
            force_refresh: Force re-scrape even if recent data exists

        Returns:
            Scrape response dict with allocations
        """
        response = await self.post(
            "/scrape/bravos",
            json={"force_refresh": force_refresh},
        )
        return response.json()

    async def scrape_sleeve(
        self, sleeve_name: str, force_refresh: bool = False
    ) -> dict[str, Any]:
        """
        Scrape a generic sleeve's portfolio data.

        Args:
            sleeve_name: Name of the sleeve to scrape
            force_refresh: Force re-scrape even if recent data exists

        Returns:
            Scrape response dict with allocations
        """
        response = await self.post(
            f"/scrape/{sleeve_name}",
            json={"force_refresh": force_refresh},
        )
        return response.json()


# Singleton instance for convenience
_browser_worker_client: BrowserWorkerClient | None = None


def get_browser_worker_client() -> BrowserWorkerClient:
    """
    Get the browser worker client singleton.

    Returns:
        BrowserWorkerClient instance
    """
    global _browser_worker_client
    if _browser_worker_client is None:
        _browser_worker_client = BrowserWorkerClient()
    return _browser_worker_client


async def close_browser_worker_client():
    """Close the browser worker client."""
    global _browser_worker_client
    if _browser_worker_client is not None:
        await _browser_worker_client.close()
        _browser_worker_client = None
