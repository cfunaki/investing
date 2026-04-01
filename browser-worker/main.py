"""
Browser Worker Service

A separate Cloud Run service that handles Playwright browser automation.
Isolated from the orchestrator to allow independent scaling and failure isolation.

This service exposes endpoints for scraping various investment sources.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Cloud Storage for session persistence (optional - only in production)
try:
    from google.cloud import storage
    GCS_AVAILABLE = True
except ImportError as e:
    GCS_AVAILABLE = False
    print(f"WARNING: google-cloud-storage not available: {e}")

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


# =============================================================================
# Configuration
# =============================================================================

# Paths relative to project root
PROJECT_ROOT = Path(__file__).parent.parent
DATA_PATH = PROJECT_ROOT / "data"
RAW_DATA_PATH = DATA_PATH / "raw"
SESSIONS_PATH = DATA_PATH / "sessions"

# Environment variables for Bravos credentials (set in Cloud Run secrets)
BRAVOS_BASE_URL = os.getenv("BRAVOS_BASE_URL", "https://bravosresearch.com")
BRAVOS_USERNAME = os.getenv("BRAVOS_USERNAME", "")
BRAVOS_PASSWORD = os.getenv("BRAVOS_PASSWORD", "")

# Cloud Storage for session persistence
GCS_SESSION_BUCKET = os.getenv("GCS_SESSION_BUCKET", "")
GCS_SESSION_PATH = "sessions/bravos.json"


# =============================================================================
# Models
# =============================================================================


class ScrapeRequest(BaseModel):
    """Request to scrape a sleeve's portfolio."""

    force_refresh: bool = False  # Force re-scrape even if recent data exists


class Allocation(BaseModel):
    """A single portfolio allocation."""

    symbol: str
    target_weight: float  # 0.0 to 1.0
    side: str  # 'long' or 'short'
    raw_weight: int | None = None  # Original weight value (1-20 scale)
    asset_name: str | None = None  # Full asset name


class ScrapeResponse(BaseModel):
    """Response from a scrape operation."""

    success: bool
    sleeve: str
    scraped_at: str
    last_updated: str | None = None  # When the source data was last updated
    latency_ms: int
    cold_start: bool
    allocations: list[Allocation] | None = None
    total_positions: int = 0
    error: str | None = None
    error_type: str | None = None  # 'auth', 'parse', 'timeout', 'unknown'


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: str
    node_available: bool
    session_exists: bool
    latency_info: dict[str, Any]


class LatencyMetrics(BaseModel):
    """Latency metrics for monitoring."""

    cold_start: bool
    total_ms: int
    scrape_ms: int | None = None
    parse_ms: int | None = None


# =============================================================================
# Lifespan and State
# =============================================================================

# Track cold start and timing
_cold_start = True
_last_scrape_time: float | None = None
_scrape_count = 0


# =============================================================================
# Cloud Storage Session Persistence
# =============================================================================


def download_session_from_gcs() -> bool:
    """
    Download session from Cloud Storage if available.

    Returns True if session was downloaded successfully.
    """
    if not GCS_AVAILABLE or not GCS_SESSION_BUCKET:
        return False

    try:
        client = storage.Client()
        bucket = client.bucket(GCS_SESSION_BUCKET)
        blob = bucket.blob(GCS_SESSION_PATH)

        if not blob.exists():
            logger.info("gcs_session_not_found", bucket=GCS_SESSION_BUCKET, path=GCS_SESSION_PATH)
            return False

        local_path = SESSIONS_PATH / "bravos.json"
        blob.download_to_filename(str(local_path))

        logger.info(
            "gcs_session_downloaded",
            bucket=GCS_SESSION_BUCKET,
            path=GCS_SESSION_PATH,
            local_path=str(local_path),
        )
        return True

    except Exception as e:
        logger.warning("gcs_session_download_failed", error=str(e))
        return False


def upload_session_to_gcs() -> bool:
    """
    Upload session to Cloud Storage for persistence.

    Returns True if session was uploaded successfully.
    """
    if not GCS_AVAILABLE or not GCS_SESSION_BUCKET:
        return False

    local_path = SESSIONS_PATH / "bravos.json"
    if not local_path.exists():
        return False

    try:
        client = storage.Client()
        bucket = client.bucket(GCS_SESSION_BUCKET)
        blob = bucket.blob(GCS_SESSION_PATH)

        blob.upload_from_filename(str(local_path))

        logger.info(
            "gcs_session_uploaded",
            bucket=GCS_SESSION_BUCKET,
            path=GCS_SESSION_PATH,
        )
        return True

    except Exception as e:
        logger.warning("gcs_session_upload_failed", error=str(e))
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _cold_start

    logger.info(
        "browser_worker_starting",
        cold_start=_cold_start,
        project_root=str(PROJECT_ROOT),
        gcs_bucket=GCS_SESSION_BUCKET or "not_configured",
    )

    # Ensure data directories exist
    RAW_DATA_PATH.mkdir(parents=True, exist_ok=True)
    SESSIONS_PATH.mkdir(parents=True, exist_ok=True)

    # Try to download session from Cloud Storage on startup
    if GCS_SESSION_BUCKET:
        session_downloaded = download_session_from_gcs()
        logger.info("startup_session_check", gcs_download=session_downloaded)

    yield

    logger.info("browser_worker_stopping")
    _cold_start = True


# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Browser Worker Service",
    description="Playwright browser automation for investment data scraping",
    version="0.1.0",
    lifespan=lifespan,
)


# =============================================================================
# Helper Functions
# =============================================================================


def check_node_available() -> bool:
    """Check if Node.js is available."""
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_session_exists() -> bool:
    """Check if Bravos session file exists."""
    session_file = SESSIONS_PATH / "bravos.json"
    return session_file.exists()


def parse_scraper_output(output_file: Path) -> tuple[list[Allocation], str | None, int]:
    """
    Parse the JSON output from the scraper.

    Returns:
        tuple of (allocations, last_updated, total_weight)
    """
    with open(output_file) as f:
        data = json.load(f)

    trades = data.get("trades", [])
    last_updated = data.get("lastUpdated")
    total_weight = data.get("totalWeight", 0)

    allocations = []
    for trade in trades:
        symbol = trade.get("symbol")
        weight = trade.get("weight", 0)
        action = trade.get("action", "Long")

        if symbol and total_weight > 0:
            allocations.append(
                Allocation(
                    symbol=symbol,
                    target_weight=weight / total_weight,
                    side="short" if action.lower() == "short" else "long",
                    raw_weight=weight,
                    asset_name=trade.get("asset"),
                )
            )

    return allocations, last_updated, len(trades)


# =============================================================================
# Endpoints
# =============================================================================


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Health check endpoint.

    Returns service status and diagnostic information.
    """
    global _cold_start

    node_available = check_node_available()
    session_exists = check_session_exists()

    status = "healthy"
    if not node_available:
        status = "degraded"
    if not session_exists:
        status = "needs_auth"  # Session needs to be initialized

    return HealthResponse(
        status=status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        node_available=node_available,
        session_exists=session_exists,
        latency_info={
            "cold_start": _cold_start,
            "last_scrape_time": _last_scrape_time,
            "scrape_count": _scrape_count,
            "gcs_bucket": GCS_SESSION_BUCKET or None,
            "gcs_available": GCS_AVAILABLE,
        },
    )


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "browser-worker",
        "version": "0.1.0",
        "health": "/health",
        "endpoints": ["/scrape/bravos"],
    }


@app.post("/scrape/bravos", response_model=ScrapeResponse)
async def scrape_bravos(request: ScrapeRequest | None = None) -> ScrapeResponse:
    """
    Scrape Bravos portfolio data.

    Calls the existing TypeScript Playwright scraper and returns normalized allocations.

    Latency is logged for cold start analysis.
    """
    global _cold_start, _last_scrape_time, _scrape_count

    start_time = time.time()
    was_cold_start = _cold_start
    _cold_start = False
    _scrape_count += 1

    log = logger.bind(
        sleeve="bravos",
        cold_start=was_cold_start,
        scrape_number=_scrape_count,
    )

    log.info("scrape_started")

    # Check prerequisites
    if not check_session_exists():
        latency_ms = int((time.time() - start_time) * 1000)
        log.warning("scrape_failed", error="no_session", latency_ms=latency_ms)
        return ScrapeResponse(
            success=False,
            sleeve="bravos",
            scraped_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=latency_ms,
            cold_start=was_cold_start,
            error="No Bravos session found. Run 'npm run init-session' to authenticate.",
            error_type="auth",
        )

    try:
        # Run the TypeScript scraper
        # Use scrape-active which calls scrape-active-trades.ts
        scrape_start = time.time()

        result = await asyncio.to_thread(
            subprocess.run,
            ["npx", "tsx", "scripts/scrape-active-trades.ts"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=90,  # 90 second timeout
            env={
                **os.environ,
                "BRAVOS_BASE_URL": BRAVOS_BASE_URL,
                "BRAVOS_USERNAME": BRAVOS_USERNAME,
                "BRAVOS_PASSWORD": BRAVOS_PASSWORD,
            },
        )

        scrape_ms = int((time.time() - scrape_start) * 1000)

        if result.returncode != 0:
            latency_ms = int((time.time() - start_time) * 1000)
            error_msg = result.stderr[:500] if result.stderr else "Unknown error"

            # Detect error type
            error_type = "unknown"
            if "session" in error_msg.lower() or "login" in error_msg.lower():
                error_type = "auth"
            elif "timeout" in error_msg.lower():
                error_type = "timeout"

            log.error(
                "scrape_failed",
                error=error_msg,
                error_type=error_type,
                return_code=result.returncode,
                latency_ms=latency_ms,
                scrape_ms=scrape_ms,
            )

            return ScrapeResponse(
                success=False,
                sleeve="bravos",
                scraped_at=datetime.now(timezone.utc).isoformat(),
                latency_ms=latency_ms,
                cold_start=was_cold_start,
                error=f"Scraper failed: {error_msg}",
                error_type=error_type,
            )

        # Parse the output file
        parse_start = time.time()
        output_file = RAW_DATA_PATH / "active-trades-latest.json"

        if not output_file.exists():
            latency_ms = int((time.time() - start_time) * 1000)
            log.error("scrape_failed", error="output_file_not_found", latency_ms=latency_ms)
            return ScrapeResponse(
                success=False,
                sleeve="bravos",
                scraped_at=datetime.now(timezone.utc).isoformat(),
                latency_ms=latency_ms,
                cold_start=was_cold_start,
                error="Scraper completed but output file not found",
                error_type="parse",
            )

        allocations, last_updated, total_positions = parse_scraper_output(output_file)
        parse_ms = int((time.time() - parse_start) * 1000)

        latency_ms = int((time.time() - start_time) * 1000)
        _last_scrape_time = time.time()

        log.info(
            "scrape_completed",
            latency_ms=latency_ms,
            scrape_ms=scrape_ms,
            parse_ms=parse_ms,
            positions=total_positions,
            last_updated=last_updated,
        )

        # Upload session to Cloud Storage for persistence (non-blocking)
        if GCS_SESSION_BUCKET:
            try:
                upload_session_to_gcs()
            except Exception as e:
                log.warning("session_upload_failed", error=str(e))

        return ScrapeResponse(
            success=True,
            sleeve="bravos",
            scraped_at=datetime.now(timezone.utc).isoformat(),
            last_updated=last_updated,
            latency_ms=latency_ms,
            cold_start=was_cold_start,
            allocations=allocations,
            total_positions=total_positions,
        )

    except subprocess.TimeoutExpired:
        latency_ms = int((time.time() - start_time) * 1000)
        log.error("scrape_timeout", latency_ms=latency_ms)
        return ScrapeResponse(
            success=False,
            sleeve="bravos",
            scraped_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=latency_ms,
            cold_start=was_cold_start,
            error="Scraper timed out after 90 seconds",
            error_type="timeout",
        )

    except json.JSONDecodeError as e:
        latency_ms = int((time.time() - start_time) * 1000)
        log.error("parse_error", error=str(e), latency_ms=latency_ms)
        return ScrapeResponse(
            success=False,
            sleeve="bravos",
            scraped_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=latency_ms,
            cold_start=was_cold_start,
            error=f"Failed to parse scraper output: {e}",
            error_type="parse",
        )

    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        log.exception("scrape_exception", error=str(e), latency_ms=latency_ms)
        return ScrapeResponse(
            success=False,
            sleeve="bravos",
            scraped_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=latency_ms,
            cold_start=was_cold_start,
            error=str(e),
            error_type="unknown",
        )


@app.post("/scrape/{sleeve_name}", response_model=ScrapeResponse)
async def scrape_generic(
    sleeve_name: str, request: ScrapeRequest | None = None
) -> ScrapeResponse:
    """
    Generic scrape endpoint for future sleeves.

    Currently only 'bravos' is supported.
    """
    if sleeve_name == "bravos":
        return await scrape_bravos(request)

    raise HTTPException(
        status_code=404,
        detail=f"Unknown sleeve: {sleeve_name}. Supported: bravos",
    )


@app.get("/metrics")
async def get_metrics():
    """
    Return metrics for monitoring.

    Useful for tracking cold start frequency and latency.
    """
    return {
        "scrape_count": _scrape_count,
        "last_scrape_time": _last_scrape_time,
        "cold_start_state": _cold_start,
    }
