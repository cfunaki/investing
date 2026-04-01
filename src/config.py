"""
Centralized configuration for the investing automation platform.

Uses pydantic-settings for validation and environment variable loading.
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # =========================================================================
    # Database
    # =========================================================================
    database_url: str = Field(
        description="PostgreSQL connection string (Supabase)",
        examples=["postgresql://user:pass@host:5432/db"],
    )

    # =========================================================================
    # Gmail API
    # =========================================================================
    gmail_credentials_json: Optional[str] = Field(
        default=None,
        description="Gmail API credentials as JSON string (for Cloud Run)",
    )
    gmail_credentials_path: Optional[str] = Field(
        default=None,
        description="Path to Gmail API credentials file (for local dev)",
    )
    gmail_token_path: str = Field(
        default="data/sessions/gmail_token.json",
        description="Path to store Gmail OAuth token",
    )
    gmail_token_json: Optional[str] = Field(
        default=None,
        description="Gmail OAuth token as JSON string (for Cloud Run via Secret Manager)",
    )
    email_poll_interval_seconds: int = Field(
        default=180,  # 3 minutes - reasonable for Cloud Scheduler
        description="How often to poll Gmail for new emails",
        ge=60,
        le=600,
    )

    # =========================================================================
    # Robinhood
    # =========================================================================
    rh_username: str = Field(description="Robinhood account email")
    rh_password: str = Field(description="Robinhood account password")
    rh_totp_secret: Optional[str] = Field(
        default=None,
        description="TOTP secret for 2FA (base32 encoded)",
    )

    # =========================================================================
    # Telegram
    # =========================================================================
    telegram_bot_token: str = Field(description="Telegram bot token from BotFather")
    telegram_allowed_users: list[int] = Field(
        default_factory=list,
        description="List of Telegram user IDs allowed to approve trades",
    )
    telegram_chat_id: Optional[int] = Field(
        default=None,
        description="Default chat ID for sending notifications",
    )

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def parse_allowed_users(cls, v):
        """Parse comma-separated user IDs from env var."""
        if isinstance(v, str):
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        if isinstance(v, int):
            return [v]
        if isinstance(v, list):
            return v
        return []

    # =========================================================================
    # Browser Worker
    # =========================================================================
    browser_worker_url: str = Field(
        default="http://localhost:8001",
        description="URL of the browser worker service",
    )
    browser_worker_timeout: int = Field(
        default=120,
        description="Timeout in seconds for browser worker requests",
        ge=30,
        le=300,
    )

    # =========================================================================
    # Safety Limits
    # =========================================================================
    max_trade_notional: float = Field(
        default=500.0,
        description="Maximum dollar amount for a single trade",
        ge=10.0,
    )
    max_portfolio_change_pct: float = Field(
        default=0.05,
        description="Maximum portfolio change as a fraction (0.05 = 5%)",
        ge=0.01,
        le=0.25,
    )
    market_hours_only: bool = Field(
        default=True,
        description="Only execute trades during market hours",
    )
    dry_run: bool = Field(
        default=True,
        description="Log trades but don't execute (start with True!)",
    )

    # =========================================================================
    # Approval Workflow
    # =========================================================================
    approval_expiry_minutes: int = Field(
        default=10,
        description="How long approval requests are valid",
        ge=1,
        le=60,
    )

    # =========================================================================
    # Logging
    # =========================================================================
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )

    # =========================================================================
    # Environment
    # =========================================================================
    environment: str = Field(
        default="development",
        description="Environment name (development, staging, production)",
    )

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Uses lru_cache to avoid re-reading environment variables on every call.
    """
    return Settings()


# Convenience function for dependency injection
def get_config() -> Settings:
    """Get settings for FastAPI dependency injection."""
    return get_settings()
