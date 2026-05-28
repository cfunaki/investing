"""
FastAPI application entry point for the investing automation platform.

This is the main orchestrator service that handles:
- Webhooks (email notifications, Telegram callbacks)
- Scheduled jobs (reconciliation, email polling)
- Health checks
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import httpx
from sqlalchemy import text

from src.approval.telegram import get_telegram_bot
from src.approval.workflow import get_approval_workflow
from src.config import Settings, get_config
from src.db.session import get_db_context
from src.signals.email_monitor import poll_for_bravos_emails
from src.signals.processor import ProcessingResult, get_processor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Lifespan Management
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Runs startup tasks before the app starts accepting requests,
    and cleanup tasks when the app shuts down.
    """
    # Startup
    logger.info("Starting investing automation platform...")
    config = get_config()
    logger.info(f"Environment: {config.environment}")
    logger.info(f"Dry run mode: {config.dry_run}")

    # Initialize Telegram bot for webhook mode
    try:
        bot = get_telegram_bot()
        await bot.application.initialize()
        await bot.application.start()
        logger.info("Telegram bot started for webhook mode")

        # Set up webhook if URL is configured
        if config.telegram_webhook_url:
            webhook_result = await bot.setup_webhook(config.telegram_webhook_url)
            if webhook_result:
                logger.info(f"Telegram webhook configured: {config.telegram_webhook_url}")
            else:
                logger.error("Failed to set Telegram webhook")
        else:
            logger.info("No webhook URL configured, callbacks require polling mode")
    except Exception as e:
        logger.error(f"Failed to initialize Telegram bot: {e}")

    # Download Robinhood session pickle from GCS (if available)
    try:
        from src.brokers.robinhood import download_rh_session_from_gcs
        if download_rh_session_from_gcs():
            logger.info("Robinhood session restored from GCS")
        else:
            logger.info("No Robinhood session in GCS (will need /login)")
    except Exception as e:
        logger.warning(f"Failed to download RH session from GCS: {e}")

    # Verify database connectivity and run migrations
    try:
        async with get_db_context() as db:
            await db.execute(text("SELECT 1"))
        logger.info("Database connection verified")

        from src.db.migrator import run_migrations
        await run_migrations()

        # Bootstrap ledger if empty (first-time setup)
        try:
            from src.reconciliation.bootstrap import bootstrap_ledger
            result = await bootstrap_ledger(force=False)
            logger.info(f"Ledger bootstrap: {result}")
        except Exception as e:
            logger.warning(f"Ledger bootstrap failed (non-fatal): {e}")

    except Exception as e:
        logger.error(f"Database startup failed: {e}")

    # Check browser worker connectivity (non-blocking)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{config.browser_worker_url}/health")
            if response.status_code == 200:
                logger.info("Browser worker connectivity verified")
            else:
                logger.warning(f"Browser worker returned status {response.status_code}")
    except Exception as e:
        logger.warning(f"Browser worker not reachable: {e}")

    yield

    # Shutdown
    logger.info("Shutting down investing automation platform...")

    # Shutdown Telegram bot
    try:
        bot = get_telegram_bot()
        await bot.application.stop()
        await bot.application.shutdown()
        logger.info("Telegram bot shut down")
    except Exception as e:
        logger.error(f"Error shutting down Telegram bot: {e}")


# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Investing Automation Platform",
    description="Multi-sleeve portfolio automation with human-in-the-loop approval",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware (for potential future web UI)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Response Models
# =============================================================================


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: str
    environment: str
    version: str
    checks: dict[str, Any]


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: str | None = None


class JobResponse(BaseModel):
    """Response for job execution."""

    job: str
    status: str
    started_at: str
    completed_at: str | None = None
    results: dict[str, Any] | None = None
    error: str | None = None


class SignalProcessingResponse(BaseModel):
    """Response for signal processing."""

    success: bool
    signal_id: str | None = None
    intent_id: str | None = None
    validation_passed: bool = True
    validation_issues: list[str] | None = None
    processing_time_ms: int = 0
    error: str | None = None


# =============================================================================
# Health Endpoints
# =============================================================================


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check(config: Settings = Depends(get_config)) -> HealthResponse:
    """
    Health check endpoint for Cloud Run.

    Returns the current status of the service and its dependencies.
    Cloud Run uses this to determine if the instance is healthy.
    """
    checks = {}
    overall_healthy = True

    # Check database connectivity
    try:
        async with get_db_context() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as e:
        checks["database"] = f"unhealthy: {str(e)[:100]}"
        overall_healthy = False

    # Check browser worker connectivity
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{config.browser_worker_url}/health")
            if response.status_code == 200:
                checks["browser_worker"] = "healthy"
            else:
                checks["browser_worker"] = f"unhealthy: status {response.status_code}"
    except httpx.ConnectError:
        checks["browser_worker"] = "unavailable: connection refused"
    except httpx.TimeoutException:
        checks["browser_worker"] = "unavailable: timeout"
    except Exception as e:
        checks["browser_worker"] = f"unavailable: {str(e)[:100]}"

    return HealthResponse(
        status="healthy" if overall_healthy else "degraded",
        timestamp=datetime.now(timezone.utc).isoformat(),
        environment=config.environment,
        version="0.1.0",
        checks=checks,
    )


@app.get("/", tags=["Health"])
async def root():
    """Root endpoint - redirects to health check."""
    return {"message": "Investing Automation Platform", "health": "/health"}


# =============================================================================
# Webhook Endpoints (to be implemented)
# =============================================================================


@app.post("/webhooks/email", tags=["Webhooks"])
async def email_webhook():
    """
    Handle incoming email notifications.

    Called by Cloud Scheduler or email service webhook.
    """
    # TODO: Implement email polling trigger
    raise HTTPException(status_code=501, detail="Not implemented yet")


@app.post("/webhooks/telegram", tags=["Webhooks"])
async def telegram_webhook(request_body: dict[str, Any]):
    """
    Handle Telegram bot callbacks (approval buttons, commands).

    This endpoint receives updates from Telegram when:
    - A user clicks an approval/rejection button
    - A user sends a command to the bot

    The Telegram bot token must be set as a webhook to this endpoint.
    """
    log = structlog.get_logger(__name__).bind(endpoint="telegram_webhook")

    try:
        from telegram import Update

        bot = get_telegram_bot()

        # Parse the update
        update = Update.de_json(request_body, bot.application.bot)

        if update is None:
            log.warning("invalid_telegram_update")
            raise HTTPException(status_code=400, detail="Invalid update")

        # Process the update
        await bot.application.process_update(update)

        log.info("telegram_update_processed", update_id=update.update_id)
        return {"ok": True}

    except Exception as e:
        log.exception("telegram_webhook_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Job Endpoints (to be implemented)
# =============================================================================


@app.post("/jobs/poll-email", response_model=JobResponse, tags=["Jobs"])
async def job_poll_email(background_tasks: BackgroundTasks):
    """
    Scheduled job: Poll Gmail for new Bravos emails.

    Called by Cloud Scheduler every 2-5 minutes.

    This endpoint:
    1. Polls Gmail for new Bravos emails
    2. For each new email, triggers signal processing
    3. Returns summary of what was found/processed
    """
    started_at = datetime.now(timezone.utc)
    log = structlog.get_logger(__name__).bind(job="poll_email")

    try:
        log.info("starting_email_poll")

        # Load already-processed message IDs from database
        from src.signals.bravos_detector import get_bravos_detector
        try:
            detector = get_bravos_detector()
            processed_ids = await detector.get_processed_message_ids()
            log.info("loaded_processed_ids", count=len(processed_ids))
        except Exception as e:
            log.warning("failed_to_load_processed_ids", error=str(e))
            processed_ids = set()

        # Poll for new emails
        emails = await poll_for_bravos_emails(processed_ids=processed_ids)

        if not emails:
            log.info("no_new_emails")
            return JobResponse(
                job="poll_email",
                status="completed",
                started_at=started_at.isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                results={
                    "emails_found": 0,
                    "signals_created": 0,
                },
            )

        # Process each email
        processor = get_processor()
        results = []
        success_count = 0
        error_count = 0

        for email_data in emails:
            message_id = email_data["message_id"]
            payload = email_data["payload"]

            log.info(
                "processing_email",
                message_id=message_id,
                subject=payload.get("subject"),
            )

            try:
                result = await processor.process_bravos_email(
                    email_message_id=message_id,
                    email_payload=payload,
                )

                if result.success:
                    success_count += 1
                    results.append({
                        "message_id": message_id,
                        "success": True,
                        "signal_id": str(result.signal.id),
                        "intent_id": str(result.intent.id) if result.intent else None,
                    })
                else:
                    error_count += 1
                    results.append({
                        "message_id": message_id,
                        "success": False,
                        "error": result.error,
                    })

            except Exception as e:
                error_count += 1
                log.exception("email_processing_failed", message_id=message_id, error=str(e))
                results.append({
                    "message_id": message_id,
                    "success": False,
                    "error": str(e),
                })

        # Close processor
        await processor.close()

        log.info(
            "email_poll_completed",
            emails_found=len(emails),
            success=success_count,
            errors=error_count,
        )

        return JobResponse(
            job="poll_email",
            status="completed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            results={
                "emails_found": len(emails),
                "signals_created": success_count,
                "errors": error_count,
                "details": results,
            },
        )

    except Exception as e:
        log.exception("email_poll_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/jobs/reconcile", response_model=JobResponse, tags=["Jobs"])
async def job_reconcile(sleeve: str = "bravos"):
    """
    Scheduled job: Run reconciliation for a sleeve.

    Called by Cloud Scheduler every 30-60 minutes as a safety net.
    This triggers a fresh scrape and processing even without an email.

    Args:
        sleeve: Sleeve name to reconcile (default: bravos)
    """
    started_at = datetime.now(timezone.utc)
    log = structlog.get_logger(__name__).bind(job="reconcile", sleeve=sleeve)

    try:
        log.info("starting_scheduled_reconcile")

        processor = get_processor()

        # Process as a scheduled check (not email-triggered)
        result = await processor.process_scheduled_check(
            sleeve_name=sleeve,
        )

        await processor.close()

        if result.success:
            log.info(
                "reconcile_completed",
                signal_id=str(result.signal.id),
                intent_id=str(result.intent.id) if result.intent else None,
                processing_time_ms=result.processing_time_ms,
            )

            return JobResponse(
                job="reconcile",
                status="completed",
                started_at=started_at.isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                results={
                    "sleeve": sleeve,
                    "signal_id": str(result.signal.id),
                    "intent_id": str(result.intent.id) if result.intent else None,
                    "positions": result.intent.position_count if result.intent else 0,
                    "validation_passed": result.validation_passed,
                    "validation_issues": result.validation_issues,
                    "processing_time_ms": result.processing_time_ms,
                    "fetch_time_ms": result.fetch_time_ms,
                },
            )
        else:
            log.error(
                "reconcile_failed",
                error=result.error,
                error_type=result.error_type,
            )

            return JobResponse(
                job="reconcile",
                status="failed",
                started_at=started_at.isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=result.error,
                results={
                    "sleeve": sleeve,
                    "error_type": result.error_type,
                },
            )

    except Exception as e:
        log.exception("reconcile_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/jobs/poll-buffett", response_model=JobResponse, tags=["Jobs"])
async def job_poll_buffett(force: bool = False):
    """
    Scheduled job: Check for new Berkshire 13F filings.

    Called by Cloud Scheduler 2x daily.
    SEC 13F filings are released quarterly, so frequent checks aren't needed.

    Args:
        force: Force reprocessing even if filing was already processed
    """
    started_at = datetime.now(timezone.utc)
    log = structlog.get_logger(__name__).bind(job="poll_buffett", force=force)

    try:
        log.info("starting_buffett_poll")

        from src.signals.buffett_processor import check_and_process_buffett

        result = await check_and_process_buffett(force=force, dry_run=False)

        if not result.success:
            log.error("buffett_poll_failed", error=result.error)
            return JobResponse(
                job="poll_buffett",
                status="failed",
                started_at=started_at.isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=result.error,
            )

        log.info(
            "buffett_poll_completed",
            new_filing=result.new_filing,
            accession_number=result.accession_number,
            trade_count=result.trade_count,
        )

        return JobResponse(
            job="poll_buffett",
            status="completed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            results={
                "new_filing": result.new_filing,
                "accession_number": result.accession_number,
                "report_date": result.report_date.isoformat() if result.report_date else None,
                "trade_count": result.trade_count,
                "total_buy": result.total_buy,
                "total_sell": result.total_sell,
                "approval_sent": result.approval_sent,
            },
        )

    except Exception as e:
        log.exception("buffett_poll_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/jobs/expire-approvals", response_model=JobResponse, tags=["Jobs"])
async def job_expire_approvals():
    """
    Scheduled job: Mark expired approval requests.

    Called by Cloud Scheduler every 5 minutes.
    This ensures pending approvals don't linger indefinitely.
    """
    started_at = datetime.now(timezone.utc)
    log = structlog.get_logger(__name__).bind(job="expire_approvals")

    try:
        log.info("starting_approval_expiration")

        workflow = get_approval_workflow()
        expired_count = await workflow.expire_pending_approvals()

        log.info("approval_expiration_completed", expired_count=expired_count)

        return JobResponse(
            job="expire_approvals",
            status="completed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            results={
                "expired_count": expired_count,
            },
        )

    except Exception as e:
        log.exception("approval_expiration_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Manual Trigger Endpoints (for testing)
# =============================================================================


@app.post("/trigger/test-approval", tags=["Trigger"])
async def trigger_test_approval(
    sleeve: str = "bravos",
    notional: float = 100.0,
    config: Settings = Depends(get_config),
):
    """
    Send a test approval request to Telegram.

    This is useful for testing the Telegram integration without
    running the full pipeline.

    Args:
        sleeve: Sleeve name for the test
        notional: Total notional amount for test trades
    """
    from datetime import timedelta
    from uuid import uuid4

    from src.approval.telegram import ApprovalRequest, generate_approval_code

    log = structlog.get_logger(__name__).bind(endpoint="trigger_test_approval")

    bot = get_telegram_bot()
    approval_code = generate_approval_code()

    # Create a test approval request
    test_trades = [
        {"symbol": "AAPL", "side": "buy", "notional": notional * 0.4, "delta_weight": 0.02},
        {"symbol": "GOOGL", "side": "buy", "notional": notional * 0.3, "delta_weight": 0.015},
        {"symbol": "MSFT", "side": "sell", "notional": notional * 0.3, "delta_weight": -0.015},
    ]

    request = ApprovalRequest(
        approval_id=uuid4(),
        reconciliation_id=uuid4(),
        sleeve_name=sleeve,
        proposed_trades=test_trades,
        total_notional=notional,
        approval_code=approval_code,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=config.approval_expiry_minutes),
    )

    log.info("sending_test_approval", approval_code=approval_code)

    message_id = await bot.send_approval_request(request)

    if message_id:
        log.info("test_approval_sent", message_id=message_id)
        return {
            "success": True,
            "approval_code": approval_code,
            "message_id": message_id,
            "expires_at": request.expires_at.isoformat(),
        }
    else:
        log.error("failed_to_send_test_approval")
        return {
            "success": False,
            "error": "Failed to send approval request. Check Telegram configuration.",
        }


@app.post("/trigger/test-review-alert", tags=["Trigger"])
async def trigger_test_review_alert(
    sleeve: str = "bravos",
    reason: str = "Test manual review alert",
    config: Settings = Depends(get_config),
):
    """
    Send a test manual review alert to Telegram.

    Args:
        sleeve: Sleeve name for the test
        reason: Review reason to display
    """
    from uuid import uuid4

    log = structlog.get_logger(__name__).bind(endpoint="trigger_test_review_alert")

    bot = get_telegram_bot()
    intent_id = str(uuid4())

    log.info("sending_test_review_alert")

    message_id = await bot.send_review_alert(
        sleeve_name=sleeve,
        reason=reason,
        intent_id=intent_id,
        details={
            "positions": 5,
            "confidence": "75%",
            "total_weight": "98.5%",
        },
    )

    if message_id:
        log.info("test_review_alert_sent", message_id=message_id)
        return {
            "success": True,
            "intent_id": intent_id,
            "message_id": message_id,
        }
    else:
        log.error("failed_to_send_test_review_alert")
        return {
            "success": False,
            "error": "Failed to send review alert. Check Telegram configuration.",
        }


# =============================================================================
# Debug Endpoints (development only)
# =============================================================================


@app.get("/debug/config", tags=["Debug"])
async def debug_config(config: Settings = Depends(get_config)):
    """
    Show current configuration (development only).

    Excludes sensitive values.
    """
    if config.is_production:
        raise HTTPException(status_code=403, detail="Not available in production")

    return {
        "environment": config.environment,
        "dry_run": config.dry_run,
        "max_trade_notional": config.max_trade_notional,
        "max_portfolio_change_pct": config.max_portfolio_change_pct,
        "market_hours_only": config.market_hours_only,
        "approval_expiry_minutes": config.approval_expiry_minutes,
        "email_poll_interval_seconds": config.email_poll_interval_seconds,
        "browser_worker_url": config.browser_worker_url,
        "telegram_webhook_url": config.telegram_webhook_url,
    }


@app.get("/debug/webhook", tags=["Debug"])
async def debug_webhook():
    """Get current Telegram webhook configuration."""
    bot = get_telegram_bot()
    info = await bot.get_webhook_info()
    return info


@app.get("/debug/positions", tags=["Debug"])
async def debug_positions():
    """Return current Robinhood positions with current prices and values."""
    from src.brokers.robinhood import get_robinhood_adapter

    broker = get_robinhood_adapter()
    if not await broker.is_connected():
        connected = await broker.connect()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to Robinhood. Use /login first.",
            )

    positions = await broker.get_positions()

    # Get current quotes for market values
    symbols = [p.symbol for p in positions]
    quotes = await broker.get_quotes(symbols) if symbols else {}

    result = []
    total_value = 0.0
    for p in positions:
        quote = quotes.get(p.symbol)
        price = float(quote.last) if quote and quote.last else float(p.average_cost)
        qty = float(p.quantity)
        value = price * qty
        total_value += value
        result.append({
            "symbol": p.symbol,
            "quantity": qty,
            "avg_cost": float(p.average_cost),
            "current_price": price,
            "market_value": round(value, 2),
            "cost_basis": round(qty * float(p.average_cost), 2),
            "unrealized_pl": round(value - (qty * float(p.average_cost)), 2),
        })

    result.sort(key=lambda x: x["market_value"], reverse=True)

    return {
        "position_count": len(result),
        "total_market_value": round(total_value, 2),
        "positions": result,
    }


@app.post("/debug/webhook", tags=["Debug"])
async def set_webhook(webhook_url: str):
    """Manually set the Telegram webhook URL."""
    bot = get_telegram_bot()
    result = await bot.setup_webhook(webhook_url)
    if result:
        return {"success": True, "webhook_url": webhook_url}
    else:
        raise HTTPException(status_code=500, detail="Failed to set webhook")


@app.get("/admin/sleeve/{name}", tags=["Admin"])
async def admin_get_sleeve(name: str):
    """Return sleeve config row (unit_size, allocation, etc)."""
    from decimal import Decimal
    from src.db.repositories.sleeve_repository import sleeve_repository

    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, name)
        if not sleeve:
            raise HTTPException(status_code=404, detail=f"Sleeve '{name}' not found")
        return {
            "id": str(sleeve.id),
            "name": sleeve.name,
            "adapter_type": sleeve.adapter_type,
            "enabled": sleeve.enabled,
            "allocation_mode": sleeve.allocation_mode,
            "allocation_value": float(sleeve.allocation_value) if sleeve.allocation_value else None,
            "unit_size": float(sleeve.unit_size),
            "approval_required": sleeve.approval_required,
        }


@app.post("/admin/sleeve/{name}/unit-size", tags=["Admin"])
async def admin_set_unit_size(name: str, unit_size: float):
    """Update a sleeve's unit_size. Used to scale rebalance trade sizes."""
    from decimal import Decimal
    from sqlalchemy import update
    from src.db.models import Sleeve
    from src.db.repositories.sleeve_repository import sleeve_repository

    if unit_size <= 0:
        raise HTTPException(status_code=400, detail="unit_size must be > 0")

    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, name)
        if not sleeve:
            raise HTTPException(status_code=404, detail=f"Sleeve '{name}' not found")
        old = float(sleeve.unit_size)
        await db.execute(
            update(Sleeve).where(Sleeve.id == sleeve.id).values(unit_size=Decimal(str(unit_size)))
        )
        await db.commit()
        return {"sleeve": name, "unit_size_old": old, "unit_size_new": unit_size}


@app.post("/admin/sleeve/{name}/trim", tags=["Admin"])
async def admin_trim_sleeve(name: str, factor: float = 0.1):
    """Generate an approval that sells `factor` of each ledger position in
    the sleeve. User taps Approve in Telegram to execute, just like a normal
    Bravos rebalance approval. factor=0.1 means trim 10% from each."""
    from datetime import timedelta
    from decimal import Decimal
    from src.approval.telegram import (
        ApprovalRequest,
        generate_approval_code,
        get_telegram_bot,
    )
    from src.approval.workflow import get_approval_workflow
    from src.brokers.robinhood import get_robinhood_adapter
    from src.config import get_settings
    from src.db.repositories.approval_repository import approval_repository
    from src.db.repositories.reconciliation_repository import reconciliation_repository
    from src.db.repositories.sleeve_repository import sleeve_repository
    from src.db.repositories.sleeve_position_repository import sleeve_position_repository
    import structlog

    log = structlog.get_logger(__name__).bind(endpoint="admin_trim_sleeve")

    if factor <= 0 or factor >= 1:
        raise HTTPException(status_code=400, detail="factor must be in (0,1)")

    settings = get_settings()
    broker = get_robinhood_adapter()
    if not await broker.is_connected():
        if not await broker.connect():
            raise HTTPException(status_code=503, detail="RH unreachable - use /login")

    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, name)
        if not sleeve:
            raise HTTPException(status_code=404, detail=f"Sleeve {name} not found")
        positions = await sleeve_position_repository.get_by_sleeve(db, sleeve.id)

    if not positions:
        raise HTTPException(status_code=400, detail="No positions in sleeve")

    symbols = [p.symbol for p in positions]
    quotes = await broker.get_quotes(symbols)

    trades = []
    for pos in positions:
        sell_qty = round(float(pos.shares) * factor, 6)
        if sell_qty <= 0:
            continue
        quote = quotes.get(pos.symbol)
        price = float(quote.last) if quote and quote.last else 0.0
        trades.append({
            "symbol": pos.symbol,
            "side": "sell",
            "notional": 0,
            "quantity": sell_qty,
            "proposal_price": price,
            "delta_weight": -factor,
            "target_weight": 0,
            "rationale": f"Manual trim {factor * 100:.0f}%",
        })

    if not trades:
        raise HTTPException(status_code=400, detail="No trades generated")

    approval_code = generate_approval_code()
    requested_at = datetime.now(timezone.utc)
    expires_at = requested_at + timedelta(minutes=settings.approval_expiry_minutes)

    async with get_db_context() as db:
        source_event_id = f"trim_{approval_code}_{requested_at.timestamp()}"
        signal, intent, recon = await reconciliation_repository.create_full_chain(
            db=db,
            sleeve_id=sleeve.id,
            source_event_id=source_event_id,
            event_type="manual_trim",
            proposed_trades=trades,
            holdings_snapshot={},
            target_allocations={},
        )
        db_approval = await approval_repository.create(
            db=db,
            reconciliation_id=recon.id,
            approval_code=approval_code,
            proposed_trades=trades,
            requested_at=requested_at,
            expires_at=expires_at,
        )
        approval_id = db_approval.id

    workflow = get_approval_workflow()
    sleeve_mv_before, portfolio_mv = await workflow._compute_sleeve_context(name, log)

    total_proceeds_est = sum(t["quantity"] * t["proposal_price"] for t in trades)
    bot = get_telegram_bot()
    request = ApprovalRequest(
        approval_id=approval_id,
        reconciliation_id=recon.id,
        sleeve_name=name,
        proposed_trades=trades,
        total_notional=total_proceeds_est,
        approval_code=approval_code,
        expires_at=expires_at,
        sleeve_mv_before=sleeve_mv_before,
        portfolio_mv=portfolio_mv,
    )
    message_id = await bot.send_approval_request(request)

    return {
        "approval_code": approval_code,
        "approval_id": str(approval_id),
        "trade_count": len(trades),
        "estimated_proceeds": round(total_proceeds_est, 2),
        "telegram_message_id": message_id,
    }


@app.post("/admin/sleeve/{name}/consolidate-approvals", tags=["Admin"])
async def admin_consolidate_approvals(name: str):
    """Merge all pending approvals for the sleeve into a single new approval
    and cancel the originals. User then only has to tap one button."""
    from datetime import timedelta
    from src.approval.telegram import (
        ApprovalRequest,
        generate_approval_code,
        get_telegram_bot,
    )
    from src.approval.workflow import get_approval_workflow
    from src.config import get_settings
    from src.db.repositories.approval_repository import approval_repository
    from src.db.repositories.reconciliation_repository import reconciliation_repository
    from src.db.repositories.sleeve_repository import sleeve_repository
    import structlog

    log = structlog.get_logger(__name__).bind(endpoint="consolidate_approvals")
    settings = get_settings()

    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, name)
        if not sleeve:
            raise HTTPException(status_code=404, detail=f"Sleeve {name} not found")

        all_pending = await approval_repository.get_pending(db)
        # Filter to approvals whose reconciliation belongs to this sleeve
        sleeve_pending = []
        for ap in all_pending:
            sleeve_id = await approval_repository.get_sleeve_id_for_approval(db, ap.id)
            if sleeve_id == sleeve.id:
                sleeve_pending.append(ap)

    if not sleeve_pending:
        raise HTTPException(status_code=400, detail="No pending approvals to consolidate")
    if len(sleeve_pending) == 1:
        return {"note": "Only one pending approval; nothing to consolidate",
                "approval_code": sleeve_pending[0].approval_code}

    # Merge trades by (symbol, side). On overlap, the more recently created
    # approval wins (its trade replaces the earlier one). Summing across stale
    # approvals double-counts the same intent.
    sleeve_pending_sorted = sorted(sleeve_pending, key=lambda a: a.requested_at)
    combined: dict[tuple[str, str], dict] = {}
    for ap in sleeve_pending_sorted:
        for trade in ap.proposed_trades:
            key = (trade.get("symbol", "?"), trade.get("side", "?").lower())
            combined[key] = dict(trade)

    trades = list(combined.values())
    if not trades:
        raise HTTPException(status_code=400, detail="No trades after merge")

    approval_code = generate_approval_code()
    requested_at = datetime.now(timezone.utc)
    expires_at = requested_at + timedelta(minutes=settings.approval_expiry_minutes)

    async with get_db_context() as db:
        source_event_id = f"consolidate_{approval_code}_{requested_at.timestamp()}"
        signal, intent, recon = await reconciliation_repository.create_full_chain(
            db=db,
            sleeve_id=sleeve.id,
            source_event_id=source_event_id,
            event_type="consolidated_rebalance",
            proposed_trades=trades,
            holdings_snapshot={},
            target_allocations={},
        )
        db_approval = await approval_repository.create(
            db=db,
            reconciliation_id=recon.id,
            approval_code=approval_code,
            proposed_trades=trades,
            requested_at=requested_at,
            expires_at=expires_at,
        )
        approval_id = db_approval.id

        # Cancel the originals so the user doesn't tap them
        for ap in sleeve_pending:
            await approval_repository.update_status(
                db=db, approval_id=ap.id, status="cancelled",
                approved_by="consolidate", responded_at=requested_at,
            )

    workflow = get_approval_workflow()
    sleeve_mv_before, portfolio_mv = await workflow._compute_sleeve_context(name, log)

    total_buy = sum(abs(t.get("notional") or 0) for t in trades if (t.get("side") or "").lower() == "buy")
    total_notional = total_buy
    bot = get_telegram_bot()
    request = ApprovalRequest(
        approval_id=approval_id,
        reconciliation_id=recon.id,
        sleeve_name=name,
        proposed_trades=trades,
        total_notional=total_notional,
        approval_code=approval_code,
        expires_at=expires_at,
        sleeve_mv_before=sleeve_mv_before,
        portfolio_mv=portfolio_mv,
    )
    message_id = await bot.send_approval_request(request)

    return {
        "approval_code": approval_code,
        "approval_id": str(approval_id),
        "trade_count": len(trades),
        "cancelled_approvals": [ap.approval_code for ap in sleeve_pending],
        "telegram_message_id": message_id,
    }


@app.post("/admin/approval/{approval_code_or_id}/cancel", tags=["Admin"])
async def admin_cancel_approval(approval_code_or_id: str):
    """Cancel a pending approval by code (8 hex chars) or full UUID."""
    from uuid import UUID
    from src.db.repositories.approval_repository import approval_repository

    async with get_db_context() as db:
        try:
            approval_id = UUID(approval_code_or_id)
        except ValueError:
            ap = await approval_repository.get_by_code(db, approval_code_or_id)
            if not ap:
                raise HTTPException(status_code=404, detail=f"Approval {approval_code_or_id} not found")
            approval_id = ap.id

        await approval_repository.update_status(
            db=db, approval_id=approval_id,
            status="cancelled", approved_by="admin",
            responded_at=datetime.now(timezone.utc),
        )

    return {"approval_id": str(approval_id), "status": "cancelled"}


@app.post("/admin/approval/{approval_id}/execute", tags=["Admin"])
async def admin_execute_approval(approval_id: str):
    """Force execution of a pending approval. Used when a Telegram callback
    fails (e.g. due to Cloud Run instance recycle losing in-memory state),
    leaving a valid DB approval that the user cannot tap through."""
    from uuid import UUID
    from src.approval.workflow import get_approval_workflow

    workflow = get_approval_workflow()
    try:
        await workflow._handle_approval(
            approval_id=UUID(approval_id),
            approved_by="admin",
            user_name="admin",
        )
        return {"approval_id": approval_id, "status": "execution_triggered"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/sleeve/{name}/positions", tags=["Admin"])
async def admin_get_positions(name: str):
    """List sleeve ledger positions (qty + weight) for a sleeve."""
    from src.db.repositories.sleeve_repository import sleeve_repository
    from src.db.repositories.sleeve_position_repository import sleeve_position_repository

    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, name)
        if not sleeve:
            raise HTTPException(status_code=404, detail=f"Sleeve '{name}' not found")
        positions = await sleeve_position_repository.get_by_sleeve(db, sleeve.id)
        return {
            "sleeve": name,
            "positions": [
                {
                    "symbol": p.symbol,
                    "shares": float(p.shares),
                    "weight": float(p.weight) if p.weight else None,
                    "cost_basis": float(p.cost_basis) if p.cost_basis else None,
                }
                for p in positions
            ],
        }


@app.post("/admin/sleeve/{name}/positions/sync-from-rh", tags=["Admin"])
async def admin_sync_positions_from_rh(name: str, add_symbols: str = ""):
    """Reconcile sleeve_positions to actual Robinhood holdings.

    For each symbol currently in the sleeve ledger, look up its real Robinhood
    quantity. If RH shows 0 (fully sold), delete the ledger row. If non-zero,
    update the ledger qty to match. Symbols held in RH but not in this sleeve
    are left alone (they belong to other sleeves or are external).

    Pass add_symbols=SYM1,SYM2,... to also create ledger rows for those
    symbols by reading their current RH quantity. Used to recover from
    queued-execution fills that never wrote back to the ledger.
    """
    from decimal import Decimal
    from src.brokers.robinhood import get_robinhood_adapter
    from src.db.repositories.sleeve_repository import sleeve_repository
    from src.db.repositories.sleeve_position_repository import sleeve_position_repository

    broker = get_robinhood_adapter()
    if not await broker.is_connected():
        connected = await broker.connect()
        if not connected:
            raise HTTPException(status_code=503, detail="Cannot connect to Robinhood. Use /login first.")

    rh_positions = await broker.get_positions()
    rh_qty = {p.symbol: float(p.quantity) for p in rh_positions}

    changes = []
    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, name)
        if not sleeve:
            raise HTTPException(status_code=404, detail=f"Sleeve '{name}' not found")
        ledger = await sleeve_position_repository.get_by_sleeve(db, sleeve.id)
        ledger_symbols = {p.symbol for p in ledger}

        for pos in ledger:
            real_qty = rh_qty.get(pos.symbol, 0.0)
            old_qty = float(pos.shares)
            if abs(real_qty - old_qty) < 1e-6:
                continue
            if real_qty <= 0:
                await sleeve_position_repository.delete_position(db, sleeve.id, pos.symbol)
                changes.append({"symbol": pos.symbol, "action": "deleted", "old_qty": old_qty, "new_qty": 0.0})
            else:
                await sleeve_position_repository.update_position(db, pos, shares=Decimal(str(real_qty)))
                changes.append({"symbol": pos.symbol, "action": "updated", "old_qty": old_qty, "new_qty": real_qty})

        # Optionally create rows for new symbols missing from the ledger
        wanted = [s.strip().upper() for s in add_symbols.split(",") if s.strip()]
        for sym in wanted:
            if sym in ledger_symbols:
                continue
            real_qty = rh_qty.get(sym, 0.0)
            if real_qty <= 0:
                changes.append({"symbol": sym, "action": "skipped", "reason": "not in RH"})
                continue
            await sleeve_position_repository.create_position(
                db, sleeve.id, sym, shares=Decimal(str(real_qty))
            )
            changes.append({"symbol": sym, "action": "created", "old_qty": 0.0, "new_qty": real_qty})

        await db.commit()

    return {"sleeve": name, "changes": changes}


# =============================================================================
# Bravos Scheduler Endpoint
# =============================================================================


@app.post("/jobs/poll-bravos", response_model=JobResponse, tags=["Jobs"])
async def job_poll_bravos(force: bool = False, skip_scrape: bool = False):
    """
    Scheduled job: Check for new Bravos emails and process.

    Called by Cloud Scheduler every 3 hours.

    Args:
        force: Force processing even if email was already seen
        skip_scrape: Skip scraping (use cached data)
    """
    started_at = datetime.now(timezone.utc)
    log = structlog.get_logger(__name__).bind(job="poll_bravos", force=force)

    try:
        log.info("starting_bravos_poll")

        from src.signals.bravos_processor import get_bravos_processor

        processor = get_bravos_processor()
        result = await processor.check_and_process(
            force=force,
            dry_run=False,
            skip_scrape=skip_scrape,
        )

        if not result.success:
            log.error("bravos_poll_failed", error=result.error)
            return JobResponse(
                job="poll_bravos",
                status="failed",
                started_at=started_at.isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=result.error,
            )

        log.info(
            "bravos_poll_completed",
            new_email=result.new_email,
            trade_count=result.trade_count,
        )

        return JobResponse(
            job="poll_bravos",
            status="completed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            results={
                "new_email": result.new_email,
                "message_id": result.message_id,
                "subject": result.subject,
                "trade_count": result.trade_count,
                "total_buy": result.total_buy,
                "total_sell": result.total_sell,
                "approval_sent": result.approval_sent,
            },
        )

    except Exception as e:
        log.exception("bravos_poll_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/jobs/execute-queued", response_model=JobResponse, tags=["Jobs"])
async def job_execute_queued():
    """
    Scheduled job: Execute trades queued for market open.

    Called by Cloud Scheduler at 9:31 AM ET on weekdays.
    """
    started_at = datetime.now(timezone.utc)
    log = structlog.get_logger(__name__).bind(job="execute_queued")

    try:
        log.info("starting_queued_execution")

        from src.db.repositories.state_repository import state_repository
        from src.db.session import get_db_context
        from src.execution.executor import execute_approved_trades

        async with get_db_context() as db:
            pending = await state_repository.get_pending_executions(
                db, before=datetime.now(timezone.utc)
            )

        if not pending:
            log.info("no_queued_executions")
            return JobResponse(
                job="execute_queued",
                status="completed",
                started_at=started_at.isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                results={"executed": 0, "queued_count": 0},
            )

        log.info("found_queued_executions", count=len(pending))

        executed = 0
        failed = 0
        results = []

        for queued in pending:
            try:
                # Mark as executing
                async with get_db_context() as db:
                    await state_repository.update_queued_status(
                        db, queued.id, "executing"
                    )

                # Execute the trades (from_queue=True prevents re-queuing on holidays)
                report = await execute_approved_trades(
                    approval_id=queued.approval_id,
                    trades=queued.trades,
                    from_queue=True,
                )

                if report.success:
                    async with get_db_context() as db:
                        await state_repository.update_queued_status(
                            db, queued.id, "completed"
                        )
                    executed += 1
                    results.append({
                        "queued_id": str(queued.id),
                        "success": True,
                        "executed": report.executed,
                    })
                else:
                    async with get_db_context() as db:
                        await state_repository.update_queued_status(
                            db, queued.id, "failed", error=report.error
                        )
                    failed += 1
                    results.append({
                        "queued_id": str(queued.id),
                        "success": False,
                        "error": report.error,
                    })

            except Exception as e:
                log.exception(
                    "queued_execution_failed",
                    queued_id=str(queued.id),
                    error=str(e),
                )
                async with get_db_context() as db:
                    await state_repository.update_queued_status(
                        db, queued.id, "failed", error=str(e)
                    )
                failed += 1
                results.append({
                    "queued_id": str(queued.id),
                    "success": False,
                    "error": str(e),
                })

        # Notify results
        try:
            from src.approval.telegram import get_telegram_bot
            bot = get_telegram_bot()
            await bot.send_notification(
                f"*Queued Execution Complete*\n\n"
                f"Executed: {executed}\n"
                f"Failed: {failed}",
            )
        except Exception:
            pass

        log.info("queued_execution_completed", executed=executed, failed=failed)

        return JobResponse(
            job="execute_queued",
            status="completed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            results={
                "queued_count": len(pending),
                "executed": executed,
                "failed": failed,
                "details": results,
            },
        )

    except Exception as e:
        log.exception("execute_queued_job_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
