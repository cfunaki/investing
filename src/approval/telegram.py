"""
Telegram bot integration for trade approvals and notifications.

This module handles:
- Sending approval requests with inline buttons
- Processing button callbacks
- Sending manual review alerts
- Bot commands for reviewing and managing intents
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from src.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass
class ApprovalRequest:
    """Data for an approval request."""

    approval_id: UUID
    reconciliation_id: UUID
    sleeve_name: str
    proposed_trades: list[dict[str, Any]]
    total_notional: float
    approval_code: str
    expires_at: datetime
    message_id: int | None = None
    chat_id: int | None = None


def generate_approval_code() -> str:
    """Generate a short, unique approval code."""
    return secrets.token_hex(4).upper()


def generate_callback_data(action: str, approval_code: str) -> str:
    """Generate callback data for inline buttons."""
    return f"{action}:{approval_code}"


def parse_callback_data(data: str) -> tuple[str, str]:
    """Parse callback data into action and approval code."""
    parts = data.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid callback data: {data}")
    return parts[0], parts[1]


class TelegramBot:
    """
    Telegram bot for trade approval workflow.

    Handles:
    - Sending approval requests with Approve/Reject buttons
    - Processing approval/rejection callbacks
    - Sending manual review alerts
    - Bot commands for inspection and retry
    """

    def __init__(self, token: str | None = None, allowed_users: list[int] | None = None):
        """
        Initialize the Telegram bot.

        Args:
            token: Bot token from BotFather (uses config if not provided)
            allowed_users: List of user IDs allowed to approve (uses config if not provided)
        """
        settings = get_settings()
        self.token = token or settings.telegram_bot_token
        self.allowed_users = set(allowed_users or settings.telegram_allowed_users)
        self.default_chat_id = settings.telegram_chat_id

        self._application: Application | None = None

        # In-memory store for pending approvals (in production, use database)
        self._pending_approvals: dict[str, ApprovalRequest] = {}

        # Callback handlers (set by workflow manager)
        self.on_approval: callable | None = None
        self.on_rejection: callable | None = None
        self.on_retry: callable | None = None

    def _build_application(self) -> Application:
        """Build the Telegram application with handlers."""
        app = Application.builder().token(self.token).build()

        # Command handlers
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("pending", self._cmd_pending))
        app.add_handler(CommandHandler("review", self._cmd_review))
        app.add_handler(CommandHandler("retry", self._cmd_retry))
        app.add_handler(CommandHandler("dismiss", self._cmd_dismiss))
        app.add_handler(CommandHandler("buffett", self._cmd_buffett))

        # Callback query handler for inline buttons
        app.add_handler(CallbackQueryHandler(self._handle_callback))

        return app

    @property
    def application(self) -> Application:
        """Get or create the Telegram application."""
        if self._application is None:
            self._application = self._build_application()
        return self._application

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to approve trades."""
        return user_id in self.allowed_users

    # =========================================================================
    # Message Formatting
    # =========================================================================

    def format_approval_message(
        self,
        sleeve_name: str,
        trades: list[dict[str, Any]],
        total_notional: float,
        approval_code: str,
        expires_at: datetime,
    ) -> str:
        """Format an approval request message."""
        lines = [
            f"*Trade Approval Request*",
            f"Sleeve: `{sleeve_name}`",
            f"Code: `{approval_code}`",
            "",
            "*Proposed Trades:*",
        ]

        for trade in trades[:10]:  # Limit to 10 trades in message
            symbol = trade.get("symbol", "???")
            side = trade.get("side", "???").upper()
            notional = trade.get("notional", 0)
            delta = trade.get("delta_weight", 0) * 100

            emoji = "+" if side == "BUY" else "-"
            lines.append(f"  {emoji} {symbol}: ${abs(notional):,.0f} ({delta:+.1f}%)")

        if len(trades) > 10:
            lines.append(f"  ... and {len(trades) - 10} more")

        lines.extend([
            "",
            f"*Total:* ${total_notional:,.2f}",
            f"*Expires:* {expires_at.strftime('%H:%M UTC')}",
            "",
            "Tap a button below to respond:",
        ])

        return "\n".join(lines)

    def format_review_alert(
        self,
        sleeve_name: str,
        reason: str,
        intent_id: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Format a manual review alert message."""
        lines = [
            f"*Manual Review Required*",
            f"Sleeve: `{sleeve_name}`",
            f"Intent: `{intent_id[:8]}...`",
            "",
            f"*Reason:* {reason}",
        ]

        if details:
            lines.append("")
            lines.append("*Details:*")
            for key, value in details.items():
                # Escape underscores for Telegram Markdown
                safe_key = str(key).replace("_", "\\_")
                safe_value = str(value).replace("_", "\\_")
                lines.append(f"  {safe_key}: {safe_value}")

        lines.extend([
            "",
            "Commands:",
            "  `/review` - Inspect details",
            "  `/retry` - Re-process signal",
            "  `/dismiss` - Discard this alert",
        ])

        return "\n".join(lines)

    def format_confirmation(
        self,
        action: str,
        sleeve_name: str,
        approval_code: str,
        user_name: str,
    ) -> str:
        """Format an approval/rejection confirmation message."""
        emoji = "Approved" if action == "approve" else "Rejected"
        return (
            f"*Trade {emoji}*\n"
            f"Sleeve: `{sleeve_name}`\n"
            f"Code: `{approval_code}`\n"
            f"By: {user_name}\n"
            f"At: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    # =========================================================================
    # Sending Messages
    # =========================================================================

    async def send_approval_request(
        self,
        approval_request: ApprovalRequest,
        chat_id: int | None = None,
    ) -> int | None:
        """
        Send an approval request with inline buttons.

        Args:
            approval_request: The approval request data
            chat_id: Chat ID to send to (uses default if not provided)

        Returns:
            Message ID if sent successfully, None otherwise
        """
        chat_id = chat_id or self.default_chat_id
        if not chat_id:
            logger.error("no_chat_id_for_approval")
            return None

        log = logger.bind(
            approval_code=approval_request.approval_code,
            chat_id=chat_id,
        )

        try:
            message_text = self.format_approval_message(
                sleeve_name=approval_request.sleeve_name,
                trades=approval_request.proposed_trades,
                total_notional=approval_request.total_notional,
                approval_code=approval_request.approval_code,
                expires_at=approval_request.expires_at,
            )

            # Create inline keyboard
            keyboard = [
                [
                    InlineKeyboardButton(
                        "Approve",
                        callback_data=generate_callback_data("approve", approval_request.approval_code),
                    ),
                    InlineKeyboardButton(
                        "Reject",
                        callback_data=generate_callback_data("reject", approval_request.approval_code),
                    ),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Send message
            bot = self.application.bot
            message = await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

            # Store approval request
            approval_request.message_id = message.message_id
            approval_request.chat_id = chat_id
            self._pending_approvals[approval_request.approval_code] = approval_request

            log.info("approval_request_sent", message_id=message.message_id)
            return message.message_id

        except Exception as e:
            log.exception("failed_to_send_approval", error=str(e))
            return None

    async def send_review_alert(
        self,
        sleeve_name: str,
        reason: str,
        intent_id: str,
        details: dict[str, Any] | None = None,
        chat_id: int | None = None,
    ) -> int | None:
        """
        Send a manual review alert.

        Args:
            sleeve_name: Name of the sleeve
            reason: Why review is needed
            intent_id: ID of the intent needing review
            details: Additional details
            chat_id: Chat ID to send to

        Returns:
            Message ID if sent successfully
        """
        chat_id = chat_id or self.default_chat_id
        if not chat_id:
            logger.error("no_chat_id_for_review_alert")
            return None

        log = logger.bind(intent_id=intent_id, chat_id=chat_id)

        try:
            message_text = self.format_review_alert(
                sleeve_name=sleeve_name,
                reason=reason,
                intent_id=intent_id,
                details=details,
            )

            bot = self.application.bot
            message = await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode="Markdown",
            )

            log.info("review_alert_sent", message_id=message.message_id)
            return message.message_id

        except Exception as e:
            log.exception("failed_to_send_review_alert", error=str(e))
            return None

    async def send_notification(
        self,
        text: str,
        chat_id: int | None = None,
        parse_mode: str = "Markdown",
    ) -> int | None:
        """Send a simple notification message."""
        chat_id = chat_id or self.default_chat_id
        if not chat_id:
            return None

        try:
            bot = self.application.bot
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
            return message.message_id
        except Exception as e:
            logger.exception("failed_to_send_notification", error=str(e))
            return None

    async def update_approval_message(
        self,
        approval_code: str,
        new_text: str,
    ) -> bool:
        """Update an existing approval message (remove buttons, show result)."""
        approval = self._pending_approvals.get(approval_code)
        if not approval or not approval.message_id or not approval.chat_id:
            return False

        try:
            bot = self.application.bot
            await bot.edit_message_text(
                chat_id=approval.chat_id,
                message_id=approval.message_id,
                text=new_text,
                parse_mode="Markdown",
            )
            return True
        except Exception as e:
            logger.exception("failed_to_update_message", error=str(e))
            return False

    # =========================================================================
    # Callback Handlers
    # =========================================================================

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks."""
        query = update.callback_query
        user = query.from_user

        log = logger.bind(
            user_id=user.id,
            user_name=user.username,
            callback_data=query.data,
        )

        # Acknowledge the callback
        await query.answer()

        # Check authorization
        if not self._is_authorized(user.id):
            log.warning("unauthorized_approval_attempt")
            await query.answer("You are not authorized to approve trades.", show_alert=True)
            return

        try:
            action, approval_code = parse_callback_data(query.data)
        except ValueError as e:
            log.warning("invalid_callback_data", error=str(e))
            return

        # Find the approval request
        approval = self._pending_approvals.get(approval_code)
        if not approval:
            log.warning("approval_not_found", approval_code=approval_code)
            await query.answer("This approval request has expired or was already processed.", show_alert=True)
            return

        # Check expiration
        if datetime.now(timezone.utc) > approval.expires_at:
            log.warning("approval_expired", approval_code=approval_code)
            await query.answer("This approval request has expired.", show_alert=True)
            del self._pending_approvals[approval_code]
            return

        log.info("processing_approval_callback", action=action, approval_code=approval_code)

        # Update the message
        confirmation = self.format_confirmation(
            action=action,
            sleeve_name=approval.sleeve_name,
            approval_code=approval_code,
            user_name=user.username or str(user.id),
        )
        await self.update_approval_message(approval_code, confirmation)

        # Remove from pending
        del self._pending_approvals[approval_code]

        # Call the appropriate handler
        if action == "approve" and self.on_approval:
            await self.on_approval(
                approval_id=approval.approval_id,
                approved_by=str(user.id),
                user_name=user.username,
            )
        elif action == "reject" and self.on_rejection:
            await self.on_rejection(
                approval_id=approval.approval_id,
                rejected_by=str(user.id),
                user_name=user.username,
            )

    # =========================================================================
    # Command Handlers
    # =========================================================================

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text(
                "You are not authorized to use this bot.\n"
                f"Your user ID is: {user.id}"
            )
            return

        await update.message.reply_text(
            "*Investing Automation Bot*\n\n"
            "I'll send you trade approval requests. "
            "Use the buttons to approve or reject.\n\n"
            "Commands:\n"
            "/status - Show system status\n"
            "/pending - List pending approvals\n"
            "/help - Show this help message",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        await update.message.reply_text(
            "*Available Commands*\n\n"
            "*Approvals:*\n"
            "/pending - List pending approval requests\n"
            "/status - Show system status\n\n"
            "*Buffett Sleeve:*\n"
            "/buffett - Show Buffett sleeve status\n"
            "/buffett check - Check for new 13F (dry run)\n"
            "/buffett sync - Check and process new filing\n"
            "/buffett force - Force reprocess\n\n"
            "*Manual Review:*\n"
            "/review <id> - Inspect an intent flagged for review\n"
            "/retry <id> - Re-process a signal\n"
            "/dismiss <id> - Dismiss a review alert\n\n"
            "*Other:*\n"
            "/help - Show this message",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command."""
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("Unauthorized")
            return

        pending_count = len(self._pending_approvals)
        settings = get_settings()

        await update.message.reply_text(
            f"*System Status*\n\n"
            f"Environment: `{settings.environment}`\n"
            f"Dry Run: `{settings.dry_run}`\n"
            f"Pending Approvals: `{pending_count}`\n"
            f"Max Trade: `${settings.max_trade_notional:,.0f}`\n"
            f"Approval Expiry: `{settings.approval_expiry_minutes} min`",
            parse_mode="Markdown",
        )

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /pending command - list pending approvals."""
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("Unauthorized")
            return

        if not self._pending_approvals:
            await update.message.reply_text("No pending approval requests.")
            return

        lines = ["*Pending Approvals:*\n"]
        for code, approval in self._pending_approvals.items():
            expires = approval.expires_at.strftime("%H:%M UTC")
            lines.append(
                f"  `{code}` - {approval.sleeve_name} "
                f"(${approval.total_notional:,.0f}, expires {expires})"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /review command - inspect an intent."""
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("Unauthorized")
            return

        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /review <intent_id>\n"
                "Example: /review abc123"
            )
            return

        intent_id = args[0]
        # TODO: Look up intent from database and show details
        await update.message.reply_text(
            f"Review for intent `{intent_id}` not yet implemented.\n"
            "This will show full intent details from the database.",
            parse_mode="Markdown",
        )

    async def _cmd_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /retry command - re-process a signal."""
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("Unauthorized")
            return

        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /retry <signal_id>\n"
                "Example: /retry abc123"
            )
            return

        signal_id = args[0]

        # Call retry handler if set
        if self.on_retry:
            await self.on_retry(signal_id=signal_id, requested_by=str(user.id))
            await update.message.reply_text(f"Retry requested for signal `{signal_id}`", parse_mode="Markdown")
        else:
            await update.message.reply_text("Retry handler not configured")

    async def _cmd_dismiss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /dismiss command - dismiss a review alert."""
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("Unauthorized")
            return

        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /dismiss <intent_id>\n"
                "Example: /dismiss abc123"
            )
            return

        intent_id = args[0]
        # TODO: Mark intent as dismissed in database
        await update.message.reply_text(
            f"Dismissed intent `{intent_id}`\n"
            "(Database update not yet implemented)",
            parse_mode="Markdown",
        )

    async def _cmd_buffett(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle /buffett command - Buffett sleeve management.

        Subcommands:
            /buffett - Show status
            /buffett check - Check for new 13F filing
            /buffett sync - Check and process (sends approval if new filing)
            /buffett force - Force reprocess last filing
        """
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("Unauthorized")
            return

        args = context.args
        subcommand = args[0].lower() if args else "status"

        if subcommand == "status":
            await self._buffett_status(update)
        elif subcommand == "check":
            await self._buffett_check(update, dry_run=True)
        elif subcommand == "sync":
            await self._buffett_check(update, dry_run=False)
        elif subcommand == "force":
            await self._buffett_check(update, dry_run=False, force=True)
        else:
            await update.message.reply_text(
                "*Buffett Sleeve Commands*\n\n"
                "/buffett - Show status\n"
                "/buffett check - Check for new 13F filing (dry run)\n"
                "/buffett sync - Check and send approval if new filing\n"
                "/buffett force - Force reprocess last filing\n",
                parse_mode="Markdown",
            )

    async def _buffett_status(self, update: Update):
        """Show Buffett sleeve status."""
        from src.signals.buffett_detector import get_buffett_detector

        detector = get_buffett_detector()
        status = detector.get_status()

        await update.message.reply_text(
            f"*Buffett Sleeve Status*\n\n"
            f"CIK: `{status['cik']}`\n"
            f"Last Processed: `{status['last_processed_accession'] or 'Never'}`\n"
            f"Last Checked: `{status['last_checked_at'] or 'Never'}`\n"
            f"Last Processed At: `{status['last_processed_at'] or 'Never'}`\n"
            f"History Entries: `{status['history_count']}`\n\n"
            "Commands:\n"
            "/buffett check - Check for new filing\n"
            "/buffett sync - Process new filing",
            parse_mode="Markdown",
        )

    async def _buffett_check(self, update: Update, dry_run: bool = True, force: bool = False):
        """Check for new Buffett filing and optionally process."""
        from src.signals.buffett_processor import check_and_process_buffett

        mode = "dry run" if dry_run else ("force" if force else "live")
        await update.message.reply_text(f"Checking for new 13F filing ({mode})...")

        try:
            result = await check_and_process_buffett(force=force, dry_run=dry_run)

            if not result.success:
                await update.message.reply_text(
                    f"*Check Failed*\n\nError: {result.error}",
                    parse_mode="Markdown",
                )
                return

            if not result.new_filing:
                await update.message.reply_text(
                    f"*No New Filing*\n\n"
                    f"Current accession: `{result.accession_number}`\n"
                    f"Already processed.",
                    parse_mode="Markdown",
                )
                return

            # New filing found
            lines = [
                "*New 13F Filing Detected*",
                "",
                f"Accession: `{result.accession_number}`",
                f"Report Date: `{result.report_date}`",
                f"Trades: `{result.trade_count}`",
                f"Total Buy: `${result.total_buy:,.0f}`",
                f"Total Sell: `${result.total_sell:,.0f}`",
            ]

            if dry_run:
                lines.extend([
                    "",
                    "_Dry run - no approval sent._",
                    "Use `/buffett sync` to send approval request.",
                ])
            elif result.approval_sent:
                lines.extend([
                    "",
                    "Approval request sent. Check above for buttons.",
                ])
            else:
                lines.extend([
                    "",
                    "No trades to execute.",
                ])

            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

        except Exception as e:
            logger.exception("buffett_check_failed", error=str(e))
            await update.message.reply_text(f"Error: {str(e)}")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start_polling(self):
        """Start the bot in polling mode (for local development)."""
        logger.info("starting_telegram_bot_polling")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()

    async def stop(self):
        """Stop the bot."""
        logger.info("stopping_telegram_bot")
        if self._application:
            await self._application.updater.stop()
            await self._application.stop()
            await self._application.shutdown()


# Singleton instance
_bot: TelegramBot | None = None


def get_telegram_bot() -> TelegramBot:
    """Get the Telegram bot singleton."""
    global _bot
    if _bot is None:
        _bot = TelegramBot()
    return _bot
