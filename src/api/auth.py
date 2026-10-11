"""
API-key authentication for the orchestrator.

Threat model: the Cloud Run service URL is effectively public (allUsers had
invoker rights), and several /admin routes can create trade requests or
execute approvals directly, bypassing the Telegram human-in-the-loop gate.
This module closes that hole at the application layer.

Two keys, two scopes (both sent as the ``X-API-Key`` header):
- ``API_KEY`` (admin): full access — /admin/*, /trigger/*, /debug/*, /jobs/*.
- ``AI_API_KEY`` (ai): scoped access for the external AI assistant —
  read positions/sleeve state and submit custom buys/sells only.
  No approval execution, no config changes, no cancellations.
  Rate-limited per hour with a daily notional cap.

Fail-closed: in production, if ``API_KEY`` is not configured, every
protected route is denied. In non-production the routes stay open so local
dev and tests keep working (a warning is logged once).

The Telegram webhook cannot carry an API key, so it is secured separately
with Telegram's ``secret_token`` mechanism (``X-Telegram-Bot-Api-Secret-Token``
header) when ``TELEGRAM_WEBHOOK_SECRET`` is configured.
"""

import logging
import secrets
import time
from collections import deque
from datetime import date

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import get_settings

logger = logging.getLogger(__name__)

API_KEY_HEADER = "x-api-key"

# Paths that never require auth.
PUBLIC_PATHS = {"/health", "/"}

# Telegram webhook is authenticated via its own secret-token header.
TELEGRAM_WEBHOOK_PATH = "/webhooks/telegram"

# Prefixes that require a valid key (admin or ai).
PROTECTED_PREFIXES = ("/admin", "/trigger", "/debug", "/jobs")

# Exact (method, path-shape) pairs the AI key may access.
# Path shapes use {name} for a single sleeve-name segment.
AI_ALLOWED = {
    ("GET", "/debug/positions"),
    ("GET", "/admin/sleeve/{name}"),
    ("GET", "/admin/sleeve/{name}/positions"),
    ("POST", "/admin/sleeve/{name}/custom-sells"),
    ("POST", "/admin/sleeve/{name}/custom-buys"),
}


def _path_shape(path: str) -> str:
    """Collapse '/admin/sleeve/tactical/positions' -> '/admin/sleeve/{name}/positions'."""
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "admin" and parts[1] == "sleeve":
        parts[2] = "{name}"
    return "/" + "/".join(parts)


def _ai_allowed(method: str, path: str) -> bool:
    return (method.upper(), _path_shape(path)) in AI_ALLOWED


class _AiUsageTracker:
    """In-memory rate limiting for the AI key. Per-process; good enough for
    a single-instance Cloud Run service bounding a malfunctioning caller."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._notional_day: dict[str, date] = {}
        self._notional_used: dict[str, float] = {}

    def check_request(self, key: str, max_per_hour: int) -> bool:
        now = time.time()
        window = self._hits.setdefault(key, deque())
        while window and window[0] <= now - 3600:
            window.popleft()
        if len(window) >= max_per_hour:
            return False
        window.append(now)
        return True

    def check_notional(self, key: str, amount: float, daily_cap: float) -> bool:
        today = date.today()
        if self._notional_day.get(key) != today:
            self._notional_day[key] = today
            self._notional_used[key] = 0.0
        return self._notional_used.get(key, 0.0) + amount <= daily_cap

    def record_notional(self, key: str, amount: float) -> None:
        today = date.today()
        if self._notional_day.get(key) != today:
            self._notional_day[key] = today
            self._notional_used[key] = 0.0
        self._notional_used[key] = self._notional_used.get(key, 0.0) + amount


_tracker = _AiUsageTracker()


def ai_check_notional(amount: float) -> None:
    """Call from submit endpoints before creating an approval. Raises 429 if
    the AI key's daily notional budget is exhausted."""
    from fastapi import HTTPException

    settings = get_settings()
    key = settings.ai_api_key or ""
    if not _tracker.check_notional(key, amount, settings.ai_max_daily_notional):
        raise HTTPException(
            status_code=429,
            detail=(
                "AI daily notional cap exceeded "
                f"(${settings.ai_max_daily_notional:,.0f}). "
                "Wait for the cap to reset or ask the owner to submit manually."
            ),
        )


def ai_record_notional(amount: float) -> None:
    settings = get_settings()
    _tracker.record_notional(settings.ai_api_key or "", amount)


def _key_matches(provided: str | None, expected: str | None) -> bool:
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce API-key auth on protected route prefixes.

    Added in main.py via app.add_middleware(AuthMiddleware).
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in PUBLIC_PATHS:
            return await call_next(request)

        if path == TELEGRAM_WEBHOOK_PATH:
            settings = get_settings()
            secret = settings.telegram_webhook_secret
            if secret:
                provided = request.headers.get("x-telegram-bot-api-secret-token")
                if not _key_matches(provided, secret):
                    return JSONResponse(
                        status_code=403,
                        content={"error": "Invalid Telegram webhook secret"},
                    )
            return await call_next(request)

        if not path.startswith(PROTECTED_PREFIXES):
            # Unknown path: let routing 404 it, but only after the key check
            # would have applied if it were protected. Fall through open.
            return await call_next(request)

        settings = get_settings()
        provided = request.headers.get(API_KEY_HEADER)

        is_admin = _key_matches(provided, settings.api_key)
        is_ai = _key_matches(provided, settings.ai_api_key)

        if not is_admin and not is_ai:
            if settings.is_production and not settings.api_key:
                logger.error("auth_middleware_no_admin_key_configured")
            return JSONResponse(
                status_code=401,
                content={"error": "Missing or invalid X-API-Key"},
            )

        scope = "admin" if is_admin else "ai"
        if scope == "ai":
            if not _ai_allowed(request.method, path):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "AI key is scoped to position reads and "
                        "custom buy/sell submission only"
                    },
                )
            if not _tracker.check_request(
                settings.ai_api_key or "", settings.ai_max_requests_per_hour
            ):
                return JSONResponse(
                    status_code=429,
                    content={"error": "AI key rate limit exceeded"},
                )

        request.state.key_scope = scope
        return await call_next(request)
