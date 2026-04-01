"""
Pre-trade safety checks.

This module implements safety controls that must pass before
any trade can be executed:

1. Max trade notional - Cap single trade size
2. Max portfolio change - Cap total portfolio impact
3. Market hours - Only trade during market hours
4. Dry run mode - Log but don't execute

These are the last line of defense before real money moves.
"""

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

import structlog

from src.brokers.base import AccountInfo, OrderRequest, OrderSide
from src.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass
class SafetyCheckResult:
    """Result of a safety check."""

    passed: bool
    check_name: str
    message: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class SafetyReport:
    """Full safety report for a set of trades."""

    passed: bool
    checks: list[SafetyCheckResult]
    blocked_trades: list[dict[str, Any]]
    warnings: list[str]

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def get_failures(self) -> list[SafetyCheckResult]:
        return [c for c in self.checks if not c.passed]


class SafetyChecker:
    """
    Performs pre-trade safety checks.

    All checks must pass before a trade can be executed.
    This is a critical safety component.
    """

    def __init__(
        self,
        max_trade_notional: float | None = None,
        max_portfolio_change_pct: float | None = None,
        market_hours_only: bool | None = None,
        dry_run: bool | None = None,
    ):
        """
        Initialize safety checker.

        Args:
            max_trade_notional: Maximum dollar amount per trade
            max_portfolio_change_pct: Maximum portfolio change as fraction
            market_hours_only: Only allow trades during market hours
            dry_run: If True, always block execution (for testing)
        """
        settings = get_settings()

        self.max_trade_notional = max_trade_notional or settings.max_trade_notional
        self.max_portfolio_change_pct = max_portfolio_change_pct or settings.max_portfolio_change_pct
        self.market_hours_only = market_hours_only if market_hours_only is not None else settings.market_hours_only
        self.dry_run = dry_run if dry_run is not None else settings.dry_run

    def check_single_trade(
        self,
        request: OrderRequest,
        account_info: AccountInfo | None = None,
    ) -> SafetyReport:
        """
        Run safety checks on a single trade.

        Args:
            request: The order request
            account_info: Optional account info for portfolio-based checks

        Returns:
            SafetyReport with results of all checks
        """
        checks = []
        warnings = []
        blocked = []

        log = logger.bind(
            symbol=request.symbol,
            side=request.side.value,
            notional=request.notional,
            quantity=request.quantity,
        )

        # Check 1: Dry run mode
        if self.dry_run:
            checks.append(SafetyCheckResult(
                passed=False,
                check_name="dry_run",
                message="Dry run mode is enabled - no trades will be executed",
            ))
            blocked.append({"reason": "dry_run", "symbol": request.symbol})
            log.info("safety_check_dry_run_blocked")

        # Check 2: Max trade notional
        trade_notional = request.notional or 0
        if trade_notional > self.max_trade_notional:
            checks.append(SafetyCheckResult(
                passed=False,
                check_name="max_trade_notional",
                message=f"Trade notional ${trade_notional:,.2f} exceeds max ${self.max_trade_notional:,.2f}",
                details={
                    "trade_notional": trade_notional,
                    "max_allowed": self.max_trade_notional,
                },
            ))
            blocked.append({
                "reason": "exceeds_max_notional",
                "symbol": request.symbol,
                "notional": trade_notional,
            })
            log.warning("safety_check_max_notional_exceeded", trade_notional=trade_notional)
        else:
            checks.append(SafetyCheckResult(
                passed=True,
                check_name="max_trade_notional",
                details={"trade_notional": trade_notional, "max_allowed": self.max_trade_notional},
            ))

        # Check 3: Market hours
        if self.market_hours_only:
            is_open = self._is_market_open()
            if not is_open:
                checks.append(SafetyCheckResult(
                    passed=False,
                    check_name="market_hours",
                    message="Market is closed - trades only allowed during market hours",
                ))
                blocked.append({"reason": "market_closed", "symbol": request.symbol})
                log.warning("safety_check_market_closed")
            else:
                checks.append(SafetyCheckResult(
                    passed=True,
                    check_name="market_hours",
                ))
        else:
            checks.append(SafetyCheckResult(
                passed=True,
                check_name="market_hours",
                message="Market hours check disabled",
            ))

        # Check 4: Portfolio impact (if account info available)
        if account_info and account_info.portfolio_value > 0:
            impact_pct = trade_notional / account_info.portfolio_value
            if impact_pct > self.max_portfolio_change_pct:
                checks.append(SafetyCheckResult(
                    passed=False,
                    check_name="portfolio_impact",
                    message=f"Trade impact {impact_pct:.1%} exceeds max {self.max_portfolio_change_pct:.1%}",
                    details={
                        "impact_pct": impact_pct,
                        "max_allowed": self.max_portfolio_change_pct,
                        "portfolio_value": account_info.portfolio_value,
                    },
                ))
                blocked.append({
                    "reason": "exceeds_portfolio_impact",
                    "symbol": request.symbol,
                    "impact_pct": impact_pct,
                })
                log.warning("safety_check_portfolio_impact_exceeded", impact_pct=impact_pct)
            else:
                checks.append(SafetyCheckResult(
                    passed=True,
                    check_name="portfolio_impact",
                    details={"impact_pct": impact_pct},
                ))

        # Warnings (non-blocking)
        if trade_notional > self.max_trade_notional * 0.8:
            warnings.append(f"Trade notional ${trade_notional:,.2f} is near max limit")

        all_passed = all(c.passed for c in checks)

        if all_passed:
            log.info("safety_checks_passed")
        else:
            log.warning("safety_checks_failed", failures=[c.check_name for c in checks if not c.passed])

        return SafetyReport(
            passed=all_passed,
            checks=checks,
            blocked_trades=blocked,
            warnings=warnings,
        )

    def check_multiple_trades(
        self,
        requests: list[OrderRequest],
        account_info: AccountInfo | None = None,
    ) -> SafetyReport:
        """
        Run safety checks on multiple trades.

        In addition to individual checks, this also validates:
        - Total portfolio impact across all trades

        Args:
            requests: List of order requests
            account_info: Optional account info for portfolio-based checks

        Returns:
            SafetyReport with aggregated results
        """
        all_checks = []
        all_blocked = []
        all_warnings = []

        # Run individual checks
        for request in requests:
            report = self.check_single_trade(request, account_info)
            all_checks.extend(report.checks)
            all_blocked.extend(report.blocked_trades)
            all_warnings.extend(report.warnings)

        # Additional check: Total portfolio impact
        if account_info and account_info.portfolio_value > 0:
            total_notional = sum(r.notional or 0 for r in requests)
            total_impact = total_notional / account_info.portfolio_value

            if total_impact > self.max_portfolio_change_pct:
                all_checks.append(SafetyCheckResult(
                    passed=False,
                    check_name="total_portfolio_impact",
                    message=f"Total impact {total_impact:.1%} exceeds max {self.max_portfolio_change_pct:.1%}",
                    details={
                        "total_impact": total_impact,
                        "max_allowed": self.max_portfolio_change_pct,
                        "trade_count": len(requests),
                    },
                ))
            else:
                all_checks.append(SafetyCheckResult(
                    passed=True,
                    check_name="total_portfolio_impact",
                    details={"total_impact": total_impact},
                ))

        all_passed = all(c.passed for c in all_checks)

        return SafetyReport(
            passed=all_passed,
            checks=all_checks,
            blocked_trades=all_blocked,
            warnings=all_warnings,
        )

    def _is_market_open(self) -> bool:
        """
        Check if US stock market is currently open.

        Market hours: 9:30 AM - 4:00 PM Eastern Time
        """
        now = datetime.now(timezone.utc)
        weekday = now.weekday()

        # Weekend check
        if weekday >= 5:  # Saturday = 5, Sunday = 6
            return False

        # Convert to approximate Eastern time
        # This is a simplified check - doesn't handle DST precisely
        # For production, use a proper market calendar library

        hour = now.hour
        minute = now.minute
        current_minutes = hour * 60 + minute

        # Market hours in UTC (approximate):
        # EDT (summer): 13:30 - 20:00 UTC
        # EST (winter): 14:30 - 21:00 UTC
        # Use conservative bounds that work for both
        market_open_utc = 13 * 60 + 30  # 13:30 UTC
        market_close_utc = 21 * 60  # 21:00 UTC

        return market_open_utc <= current_minutes < market_close_utc

    def get_status(self) -> dict[str, Any]:
        """Get current safety configuration status."""
        return {
            "dry_run": self.dry_run,
            "max_trade_notional": self.max_trade_notional,
            "max_portfolio_change_pct": self.max_portfolio_change_pct,
            "market_hours_only": self.market_hours_only,
            "market_open": self._is_market_open(),
        }


# Singleton instance
_checker: SafetyChecker | None = None


def get_safety_checker() -> SafetyChecker:
    """Get the safety checker singleton."""
    global _checker
    if _checker is None:
        _checker = SafetyChecker()
    return _checker


def check_trade_safety(
    request: OrderRequest,
    account_info: AccountInfo | None = None,
) -> SafetyReport:
    """
    Convenience function to check a single trade.

    Args:
        request: The order request to check
        account_info: Optional account info

    Returns:
        SafetyReport with check results
    """
    checker = get_safety_checker()
    return checker.check_single_trade(request, account_info)
