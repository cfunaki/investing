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
    except Exception as e:
        logger.error(f"Failed to initialize Telegram bot: {e}")

    # Verify database connectivity
    try:
        async with get_db_context() as db:
            await db.execute(text("SELECT 1"))
        logger.info("Database connection verified")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")

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

        # TODO: Load processed message IDs from database
        # For now, we process all found emails
        # In production, query signals table for existing source_event_ids
        processed_ids: set[str] = set()

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
        return JobResponse(
            job="poll_email",
            status="failed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )


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
        return JobResponse(
            job="reconcile",
            status="failed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )


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
        return JobResponse(
            job="poll_buffett",
            status="failed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )


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
        return JobResponse(
            job="expire_approvals",
            status="failed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )


# =============================================================================
# Manual Trigger Endpoints (for testing)
# =============================================================================


@app.post("/trigger/process-bravos", response_model=SignalProcessingResponse, tags=["Trigger"])
async def trigger_process_bravos(
    trigger_id: str | None = None,
    config: Settings = Depends(get_config),
):
    """
    Manually trigger Bravos signal processing.

    This is useful for testing the full pipeline without waiting for an email.
    It simulates what happens when a Bravos email is detected.

    Args:
        trigger_id: Optional unique identifier for this trigger (for idempotency)
    """
    log = structlog.get_logger(__name__).bind(endpoint="trigger_process_bravos")

    # Generate trigger ID if not provided
    if trigger_id is None:
        trigger_id = f"manual_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    log.info("manual_trigger_started", trigger_id=trigger_id)

    try:
        processor = get_processor()

        result = await processor.process_bravos_email(
            email_message_id=trigger_id,
            email_payload={"trigger_type": "manual", "triggered_at": datetime.now(timezone.utc).isoformat()},
        )

        await processor.close()

        if result.success:
            log.info(
                "manual_trigger_completed",
                signal_id=str(result.signal.id),
                intent_id=str(result.intent.id) if result.intent else None,
            )

            return SignalProcessingResponse(
                success=True,
                signal_id=str(result.signal.id),
                intent_id=str(result.intent.id) if result.intent else None,
                validation_passed=result.validation_passed,
                validation_issues=result.validation_issues,
                processing_time_ms=result.processing_time_ms,
            )
        else:
            log.error("manual_trigger_failed", error=result.error)
            return SignalProcessingResponse(
                success=False,
                signal_id=str(result.signal.id) if result.signal else None,
                error=result.error,
                processing_time_ms=result.processing_time_ms,
            )

    except Exception as e:
        log.exception("manual_trigger_failed", error=str(e))
        return SignalProcessingResponse(
            success=False,
            error=str(e),
        )


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
    }
