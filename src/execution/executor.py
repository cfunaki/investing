"""
Trade execution orchestrator.

This module is responsible for:
1. Receiving approved trades from the workflow
2. Running safety checks
3. Checking idempotency
4. Executing trades via the broker
5. Tracking results and sending confirmations

This is where all the safety layers come together.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from src.approval.telegram import get_telegram_bot
from src.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.brokers.robinhood import get_robinhood_adapter
from src.config import get_settings
from src.execution.idempotency import (
    ExecutionRecord,
    ExecutionState,
    generate_execution_key,
    get_idempotency_tracker,
)
from src.execution.safety import SafetyReport, get_safety_checker

logger = structlog.get_logger(__name__)


@dataclass
class TradeResult:
    """Result of executing a single trade."""

    symbol: str
    side: str
    success: bool
    execution_key: str
    order_id: str | None = None
    status: str = "pending"
    filled_quantity: float = 0.0
    filled_price: float | None = None
    filled_notional: float = 0.0
    error: str | None = None
    skipped: bool = False
    skip_reason: str | None = None


@dataclass
class ExecutionReport:
    """Report from executing a batch of trades."""

    approval_id: UUID
    success: bool
    total_trades: int
    executed: int
    skipped: int
    failed: int
    results: list[TradeResult]
    safety_report: SafetyReport | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    dry_run: bool = False


class TradeExecutor:
    """
    Orchestrates trade execution with all safety layers.

    Execution flow:
    1. Validate all trades pass safety checks
    2. For each trade:
       a. Check idempotency (skip if already executed)
       b. Mark as in-progress
       c. Place order via broker
       d. Update idempotency record
    3. Send Telegram confirmation
    """

    def __init__(
        self,
        broker: BrokerAdapter | None = None,
    ):
        """
        Initialize the executor.

        Args:
            broker: Broker adapter (uses Robinhood by default)
        """
        self.broker = broker or get_robinhood_adapter()
        self.safety_checker = get_safety_checker()
        self.idempotency_tracker = get_idempotency_tracker()
        self.telegram = get_telegram_bot()

        settings = get_settings()
        self.dry_run = settings.dry_run

    async def execute_approved_trades(
        self,
        approval_id: UUID,
        trades: list[dict[str, Any]],
        from_queue: bool = False,
    ) -> ExecutionReport:
        """
        Execute a batch of approved trades.

        Args:
            approval_id: The approval ID
            trades: List of trade dicts with symbol, side, notional

        Returns:
            ExecutionReport with results of all trades
        """
        log = logger.bind(
            approval_id=str(approval_id),
            trade_count=len(trades),
        )

        log.info("starting_trade_execution")

        report = ExecutionReport(
            approval_id=approval_id,
            success=False,
            total_trades=len(trades),
            executed=0,
            skipped=0,
            failed=0,
            results=[],
            dry_run=self.dry_run,
        )

        if not trades:
            log.info("no_trades_to_execute")
            report.success = True
            report.completed_at = datetime.now(timezone.utc)
            return report

        # Convert trades to OrderRequests for safety checking
        order_requests = []
        for trade in trades:
            side = OrderSide.BUY if trade.get("side", "").lower() == "buy" else OrderSide.SELL
            order_requests.append(OrderRequest(
                symbol=trade["symbol"],
                side=side,
                notional=abs(trade.get("notional", 0)),
            ))

        # Get account info for safety checks
        account_info = None
        try:
            if await self.broker.is_connected() or await self.broker.connect():
                account_info = await self.broker.get_account_info()
        except Exception as e:
            log.warning("failed_to_get_account_info", error=str(e))

        # Run safety checks
        safety_report = self.safety_checker.check_multiple_trades(order_requests, account_info)
        report.safety_report = safety_report

        if not safety_report.passed:
            # Check if ALL failures are market_hours (not just one)
            failures = safety_report.get_failures()
            market_closed_only = (
                len(failures) > 0
                and all(f.check_name == "market_hours" for f in failures)
                and not self.dry_run
                and not from_queue  # Don't re-queue when executing from queue
            )

            if market_closed_only:
                # Queue for next market open instead of failing
                try:
                    from src.db.repositories.state_repository import state_repository
                    from src.db.session import get_db_context
                    from src.execution.safety import next_market_open

                    execute_after = next_market_open()
                    async with get_db_context() as db:
                        queued = await state_repository.queue_execution(
                            db=db,
                            approval_id=approval_id,
                            trades=trades,
                            execute_after=execute_after,
                        )

                    log.info(
                        "trades_queued_for_market_open",
                        queued_id=str(queued.id),
                        execute_after=execute_after.isoformat(),
                    )

                    # Notify via Telegram
                    try:
                        from src.approval.telegram import get_telegram_bot
                        bot = get_telegram_bot()
                        symbols = ", ".join(t["symbol"] for t in trades)
                        await bot.send_notification(
                            f"*Trades Queued*\n\n"
                            f"Market is closed. {len(trades)} trade(s) ({symbols}) "
                            f"queued for execution at {execute_after.strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
                            f"Use /cancel\\_queued to cancel.",
                        )
                    except Exception as e:
                        log.warning("queued_notification_failed", error=str(e))

                    report.success = True
                    report.error = "queued_for_market_open"
                    report.completed_at = datetime.now(timezone.utc)
                    return report
                except Exception as e:
                    log.exception("failed_to_queue_trades", error=str(e))
                    # Fall through to normal failure handling

            log.warning(
                "safety_checks_failed",
                failures=[c.check_name for c in safety_report.get_failures()],
            )

            # In dry run mode, we still "fail" but for the right reason
            if self.dry_run:
                log.info("dry_run_mode_trades_not_executed")
                for trade in trades:
                    report.results.append(TradeResult(
                        symbol=trade["symbol"],
                        side=trade.get("side", "unknown"),
                        success=False,
                        execution_key="",
                        skipped=True,
                        skip_reason="dry_run_mode",
                    ))
                    report.skipped += 1

                report.success = True  # Dry run is "successful"
                report.completed_at = datetime.now(timezone.utc)
                await self._send_dry_run_notification(approval_id, trades)
                return report

            # Real safety failure
            for trade in trades:
                report.results.append(TradeResult(
                    symbol=trade["symbol"],
                    side=trade.get("side", "unknown"),
                    success=False,
                    execution_key="",
                    skipped=True,
                    skip_reason="safety_check_failed",
                ))
                report.skipped += 1

            report.success = False
            report.error = "Safety checks failed"
            report.completed_at = datetime.now(timezone.utc)
            return report

        # Execute each trade
        for trade in trades:
            result = await self._execute_single_trade(approval_id, trade, account_info)
            report.results.append(result)

            if result.skipped:
                report.skipped += 1
            elif result.success:
                report.executed += 1
            else:
                report.failed += 1

        report.success = report.failed == 0
        report.completed_at = datetime.now(timezone.utc)

        log.info(
            "trade_execution_completed",
            success=report.success,
            executed=report.executed,
            skipped=report.skipped,
            failed=report.failed,
        )

        # Send Telegram confirmation
        await self._send_execution_confirmation(report)

        return report

    async def _execute_single_trade(
        self,
        approval_id: UUID,
        trade: dict[str, Any],
        account_info: AccountInfo | None,
    ) -> TradeResult:
        """Execute a single trade with idempotency checking."""
        symbol = trade["symbol"]
        side_str = trade.get("side", "buy").lower()
        side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
        notional = abs(trade.get("notional", 0))
        quantity = trade.get("quantity")

        log = logger.bind(
            symbol=symbol,
            side=side.value,
            notional=notional,
        )

        # Generate execution key
        execution_key = generate_execution_key(approval_id, symbol, side)

        # Check idempotency
        is_safe, reason = await self.idempotency_tracker.is_safe_to_execute(execution_key)

        if not is_safe:
            log.warning("trade_skipped_idempotency", reason=reason)
            return TradeResult(
                symbol=symbol,
                side=side.value,
                success=False,
                execution_key=execution_key,
                skipped=True,
                skip_reason=f"idempotency:{reason}",
            )

        # Get or create execution record
        record, is_new = await self.idempotency_tracker.get_or_create(
            approval_id=approval_id,
            symbol=symbol,
            side=side,
            notional=notional,
        )

        # Mark as in progress
        if not self.idempotency_tracker.mark_in_progress(execution_key):
            log.warning("failed_to_mark_in_progress")
            return TradeResult(
                symbol=symbol,
                side=side.value,
                success=False,
                execution_key=execution_key,
                skipped=True,
                skip_reason="failed_to_acquire_lock",
            )

        # Price deviation check (asymmetric: only skip unfavorable moves)
        proposal_price = trade.get("proposal_price")
        if proposal_price:
            try:
                current_quote = await self.broker.get_quote(symbol)
                if current_quote and current_quote.last:
                    pct_change = (current_quote.last - proposal_price) / proposal_price * 100
                    from src.config import get_settings
                    threshold = get_settings().price_deviation_threshold_pct

                    # Buys: skip if price went UP (paying more)
                    # Sells: skip if price went DOWN (getting less)
                    unfavorable = (
                        (side == OrderSide.BUY and pct_change > threshold)
                        or (side == OrderSide.SELL and pct_change < -threshold)
                    )

                    log.info(
                        "price_deviation_check",
                        proposal_price=proposal_price,
                        current_price=current_quote.last,
                        pct_change=round(pct_change, 2),
                        threshold=threshold,
                        unfavorable=unfavorable,
                    )

                    if unfavorable:
                        await self.idempotency_tracker.mark_failed(execution_key, f"price_deviation:{pct_change:+.1f}%")
                        return TradeResult(
                            symbol=symbol,
                            side=side.value,
                            success=False,
                            execution_key=execution_key,
                            skipped=True,
                            skip_reason=f"price_deviation:{pct_change:+.1f}%",
                        )
                else:
                    log.warning("price_deviation_check_no_quote", symbol=symbol)
            except Exception as e:
                log.warning("price_deviation_check_failed", error=str(e))
        else:
            log.info("price_deviation_check_skipped", reason="no_proposal_price")

        # Create order request
        if quantity:
            request = OrderRequest(
                symbol=symbol,
                side=side,
                quantity=float(quantity),
                order_type=OrderType.MARKET,
                client_order_id=execution_key,
            )
        else:
            request = OrderRequest(
                symbol=symbol,
                side=side,
                notional=notional,
                order_type=OrderType.MARKET,
                client_order_id=execution_key,
            )

        log.info("placing_order")

        try:
            # Ensure broker is connected
            if not await self.broker.is_connected():
                if not await self.broker.connect():
                    await self.idempotency_tracker.mark_failed(execution_key, "broker_connection_failed")
                    return TradeResult(
                        symbol=symbol,
                        side=side.value,
                        success=False,
                        execution_key=execution_key,
                        error="Failed to connect to broker",
                    )

            # Place order
            order_result = await self.broker.place_order(request)

            # Update idempotency record
            await self.idempotency_tracker.update_from_order_result(execution_key, order_result)

            if order_result.success:
                log.info(
                    "order_placed_successfully",
                    order_id=order_result.order_id,
                    status=order_result.status.value,
                )

                return TradeResult(
                    symbol=symbol,
                    side=side.value,
                    success=True,
                    execution_key=execution_key,
                    order_id=order_result.order_id,
                    status=order_result.status.value,
                    filled_quantity=order_result.filled_quantity,
                    filled_price=order_result.filled_price,
                    filled_notional=order_result.filled_notional,
                )
            else:
                log.error(
                    "order_failed",
                    error=order_result.error,
                    status=order_result.status.value,
                )

                return TradeResult(
                    symbol=symbol,
                    side=side.value,
                    success=False,
                    execution_key=execution_key,
                    status=order_result.status.value,
                    error=order_result.error,
                )

        except Exception as e:
            log.exception("order_exception", error=str(e))
            await self.idempotency_tracker.mark_failed(execution_key, str(e))

            return TradeResult(
                symbol=symbol,
                side=side.value,
                success=False,
                execution_key=execution_key,
                error=str(e),
            )

    async def _send_execution_confirmation(self, report: ExecutionReport) -> None:
        """Send execution confirmation to Telegram."""
        lines = ["*Trade Execution Report*\n"]

        if report.dry_run:
            lines.append("*Mode:* DRY RUN (no real trades)\n")

        lines.append(f"Approval: `{str(report.approval_id)[:8]}...`")
        lines.append(f"Total: {report.total_trades} trades")
        lines.append(f"Executed: {report.executed}")
        lines.append(f"Skipped: {report.skipped}")
        lines.append(f"Failed: {report.failed}")
        lines.append("")

        # Group results by outcome
        executed = [r for r in report.results if r.success]
        skipped = [r for r in report.results if r.skipped]
        failed = [r for r in report.results if not r.success and not r.skipped]

        if executed:
            lines.append("*Executed:*")
            for r in executed[:10]:
                price_str = f" @ ${r.filled_price:.2f}" if r.filled_price else ""
                lines.append(f"  {r.symbol} {r.side.upper()}: ${r.filled_notional or 0:.0f}{price_str}")
            if len(executed) > 10:
                lines.append(f"  ... and {len(executed) - 10} more")

        if skipped:
            # Separate price drift skips from other skips
            price_skips = [r for r in skipped if r.skip_reason and "price_deviation" in r.skip_reason]
            other_skips = [r for r in skipped if not r.skip_reason or "price_deviation" not in r.skip_reason]

            if price_skips:
                lines.append("")
                lines.append(f"*Skipped ({len(price_skips)} price drift):*")
                for r in price_skips:
                    deviation = r.skip_reason.split(":")[-1] if r.skip_reason else ""
                    lines.append(f"  {r.symbol} {r.side.upper()}: {deviation}")

            if other_skips:
                lines.append("")
                # Group by reason to avoid repetitive output
                reasons: dict[str, list[str]] = {}
                for r in other_skips:
                    reason = (r.skip_reason or "unknown").replace("_", " ")
                    reasons.setdefault(reason, []).append(r.symbol)
                for reason, symbols in reasons.items():
                    lines.append(f"*Skipped ({len(symbols)} - {reason}):*")
                    lines.append(f"  {', '.join(symbols)}")

        if failed:
            lines.append("")
            lines.append(f"*Failed ({len(failed)}):*")
            for r in failed[:5]:
                error_text = (r.error or r.status or "unknown").replace("_", " ")
                lines.append(f"  {r.symbol}: {error_text}")

        if report.error:
            safe_error = report.error.replace("_", " ")
            lines.append(f"\n*Error:* {safe_error}")

        message = "\n".join(lines)

        try:
            await self.telegram.send_notification(message)
        except Exception as e:
            logger.warning("failed_to_send_confirmation", error=str(e))

    async def _send_dry_run_notification(
        self,
        approval_id: UUID,
        trades: list[dict[str, Any]],
    ) -> None:
        """Send dry run notification to Telegram."""
        lines = [
            "*DRY RUN - Trades Not Executed*\n",
            f"Approval: `{str(approval_id)[:8]}...`",
            f"Would have executed {len(trades)} trades:",
            "",
        ]

        total_notional = 0
        for trade in trades[:10]:
            side = trade.get("side", "???").upper()
            notional = abs(trade.get("notional", 0))
            total_notional += notional
            emoji = "+" if side == "BUY" else "-"
            lines.append(f"  {emoji} {trade['symbol']} {side}: ${notional:,.2f}")

        if len(trades) > 10:
            lines.append(f"  ... and {len(trades) - 10} more")

        lines.append(f"\n*Total:* ${total_notional:,.2f}")
        lines.append("\n_Set DRY\\_RUN=false to enable real trading_")

        message = "\n".join(lines)

        try:
            await self.telegram.send_notification(message)
        except Exception as e:
            logger.warning("failed_to_send_dry_run_notification", error=str(e))


# Singleton instance
_executor: TradeExecutor | None = None


def get_trade_executor() -> TradeExecutor:
    """Get the trade executor singleton."""
    global _executor
    if _executor is None:
        _executor = TradeExecutor()
    return _executor


async def execute_approved_trades(
    approval_id: UUID,
    trades: list[dict[str, Any]],
    from_queue: bool = False,
) -> ExecutionReport:
    """
    Convenience function to execute approved trades.

    Args:
        approval_id: The approval ID
        trades: List of trades to execute
        from_queue: If True, skip re-queuing on market closed (prevents loops)

    Returns:
        ExecutionReport with results
    """
    executor = get_trade_executor()
    return await executor.execute_approved_trades(approval_id, trades, from_queue=from_queue)
