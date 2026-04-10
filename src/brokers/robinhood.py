"""
Robinhood broker adapter implementation.

This adapter wraps the robin_stocks library to provide
a consistent interface for the execution layer.
"""

import asyncio
import builtins
import os
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import pyotp
import robin_stocks.robinhood as rh
import structlog

# Cloud Storage for session persistence (optional)
try:
    from google.cloud import storage as gcs_storage
    GCS_AVAILABLE = True
except ImportError:
    GCS_AVAILABLE = False

from src.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    TimeInForce,
)
from src.config import get_settings

logger = structlog.get_logger(__name__)

# GCS config for RH session pickle persistence
GCS_SESSION_BUCKET = os.getenv("GCS_SESSION_BUCKET", "investing-automation-sessions")
GCS_RH_SESSION_PATH = "sessions/robinhood.pickle"
# robin_stocks stores pickle at ~/.tokens/robinhood.pickle
LOCAL_RH_PICKLE = Path.home() / ".tokens" / "robinhood.pickle"


def download_rh_session_from_gcs() -> bool:
    """Download Robinhood session pickle from GCS if available."""
    if not GCS_AVAILABLE or not GCS_SESSION_BUCKET:
        logger.info("gcs_not_available_for_rh_session")
        return False

    try:
        client = gcs_storage.Client()
        bucket = client.bucket(GCS_SESSION_BUCKET)
        blob = bucket.blob(GCS_RH_SESSION_PATH)

        if not blob.exists():
            logger.info("gcs_rh_session_not_found")
            return False

        LOCAL_RH_PICKLE.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(LOCAL_RH_PICKLE))
        logger.info("gcs_rh_session_downloaded", local_path=str(LOCAL_RH_PICKLE))
        return True

    except Exception as e:
        logger.warning("gcs_rh_session_download_failed", error=str(e))
        return False


def upload_rh_session_to_gcs() -> bool:
    """Upload Robinhood session pickle to GCS for persistence."""
    if not GCS_AVAILABLE or not GCS_SESSION_BUCKET:
        logger.info("gcs_not_available_for_rh_session_upload")
        return False

    if not LOCAL_RH_PICKLE.exists():
        logger.warning("rh_pickle_not_found", path=str(LOCAL_RH_PICKLE))
        return False

    try:
        client = gcs_storage.Client()
        bucket = client.bucket(GCS_SESSION_BUCKET)
        blob = bucket.blob(GCS_RH_SESSION_PATH)
        blob.upload_from_filename(str(LOCAL_RH_PICKLE))
        logger.info("gcs_rh_session_uploaded")
        return True

    except Exception as e:
        logger.warning("gcs_rh_session_upload_failed", error=str(e))
        return False


def _run_sync(func, *args, **kwargs):
    """Run a synchronous function in a thread pool."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, partial(func, *args, **kwargs))


class RobinhoodAdapter(BrokerAdapter):
    """
    Robinhood broker adapter using robin_stocks library.

    This adapter provides async methods by wrapping the synchronous
    robin_stocks library calls in thread pool executors.
    """

    def __init__(self):
        """Initialize the Robinhood adapter."""
        settings = get_settings()
        self._username = settings.rh_username
        self._password = settings.rh_password
        self._totp_secret = settings.rh_totp_secret
        self._connected = False
        self._login_failed = False  # Circuit breaker: True after a failed login

    @property
    def name(self) -> str:
        return "robinhood"

    # =========================================================================
    # Connection
    # =========================================================================

    async def connect(self) -> bool:
        """Authenticate with Robinhood."""
        log = logger.bind(broker="robinhood")

        # Circuit breaker: don't retry login after a failure.
        # Only /login (connect_with_telegram_mfa) resets this.
        if self._login_failed:
            log.debug("connect_skipped_circuit_breaker_open")
            return False

        log.info("connecting_to_robinhood")

        try:
            # Generate TOTP code if available
            mfa_code = None
            if self._totp_secret:
                try:
                    totp = pyotp.TOTP(self._totp_secret)
                    mfa_code = totp.now()
                    log.info("using_totp_for_mfa")
                except Exception as e:
                    log.warning("totp_generation_failed", error=str(e))

            # Login (synchronous, run in thread)
            if mfa_code:
                result = await _run_sync(
                    rh.login,
                    username=self._username,
                    password=self._password,
                    mfa_code=mfa_code,
                    store_session=True,
                )
            else:
                result = await _run_sync(
                    rh.login,
                    username=self._username,
                    password=self._password,
                    store_session=True,
                )

            if result:
                self._connected = True
                self._login_failed = False
                log.info("robinhood_connected")
                return True
            else:
                log.error("robinhood_login_failed")
                self._login_failed = True
                await self._notify_session_expired()
                return False

        except Exception as e:
            log.exception("robinhood_connection_error", error=str(e))
            self._login_failed = True
            await self._notify_session_expired()
            return False

    async def _notify_session_expired(self) -> None:
        """Send Telegram notification when RH session expires."""
        try:
            from src.approval.telegram import get_telegram_bot
            bot = get_telegram_bot()
            await bot.send_notification(
                "*Robinhood Session Expired*\n\n"
                "Login failed (session pickle expired or MFA needed).\n"
                "Use /login to re-authenticate via SMS MFA.",
            )
        except Exception as e:
            logger.warning("failed_to_send_session_expiry_notification", error=str(e))

    async def connect_with_telegram_mfa(
        self,
        send_prompt_fn,
        wait_for_reply_fn,
    ) -> bool:
        """
        Login to Robinhood using Telegram as the MFA input medium.

        Monkey-patches builtins.input() so that when robin_stocks calls
        input() for the SMS MFA code, it sends a Telegram prompt and
        waits for the user's reply.

        Args:
            send_prompt_fn: async callable(str) to send a message to Telegram
            wait_for_reply_fn: async callable() -> str to wait for user's reply

        Returns:
            True if login succeeded
        """
        log = logger.bind(broker="robinhood", method="telegram_mfa")
        log.info("starting_telegram_mfa_login")

        loop = asyncio.get_event_loop()
        original_input = builtins.input

        def patched_input(prompt=""):
            """Replacement for input() that uses Telegram."""
            log.info("mfa_input_requested", prompt=prompt)

            # Send the prompt to Telegram and wait for reply
            future = asyncio.run_coroutine_threadsafe(
                send_prompt_fn(f"*Robinhood MFA*\n\n{prompt}"),
                loop,
            )
            future.result(timeout=10)  # Wait for message to send

            # Wait for the user's reply
            reply_future = asyncio.run_coroutine_threadsafe(
                wait_for_reply_fn(),
                loop,
            )
            reply = reply_future.result(timeout=300)  # 5 minute timeout
            log.info("mfa_code_received")
            return reply.strip()

        try:
            builtins.input = patched_input

            # Monkey-patch robin_stocks' broken _validate_sherrif_id to handle
            # None API responses and device-prompt challenges gracefully.
            import robin_stocks.robinhood.authentication as rh_auth
            original_validate = rh_auth._validate_sherrif_id

            def _patched_validate(device_token, workflow_id):
                """Patched version that handles None responses from RH API."""
                from robin_stocks.robinhood.helper import request_post, request_get
                import time

                pathfinder_url = "https://api.robinhood.com/pathfinder/user_machine/"
                machine_payload = {
                    'device_id': device_token,
                    'flow': 'suv',
                    'input': {'workflow_id': workflow_id},
                }
                machine_data = request_post(
                    url=pathfinder_url, payload=machine_payload, json=True,
                )
                if not machine_data or "id" not in machine_data:
                    log.warning("sherrif_id_missing_from_response")
                    raise Exception("No verification ID returned")

                machine_id = machine_data["id"]
                inquiries_url = (
                    f"https://api.robinhood.com/pathfinder/inquiries/"
                    f"{machine_id}/user_view/"
                )

                start_time = time.time()
                while time.time() - start_time < 120:
                    time.sleep(5)
                    resp = request_get(inquiries_url)
                    if not resp:
                        continue

                    ctx = resp.get("context", {})
                    challenge = ctx.get("sheriff_challenge")
                    if not challenge:
                        continue

                    c_type = challenge.get("type")
                    c_status = challenge.get("status")
                    c_id = challenge.get("id")

                    if c_status == "validated":
                        log.info("challenge_validated")
                        break

                    if c_type == "prompt":
                        # Device approval — poll with None-safety
                        prompt_url = (
                            f"https://api.robinhood.com/push/{c_id}/"
                            f"get_prompts_status/"
                        )
                        log.info("waiting_for_device_approval")
                        while time.time() - start_time < 120:
                            time.sleep(5)
                            status = request_get(url=prompt_url)
                            if status and status.get(
                                "challenge_status"
                            ) == "validated":
                                break
                        break

                    if c_type in ["sms", "email"] and c_status == "issued":
                        code = builtins.input(
                            f"Enter the {c_type} verification code: "
                        )
                        challenge_url = (
                            f"https://api.robinhood.com/challenge/"
                            f"{c_id}/respond/"
                        )
                        cr = request_post(
                            url=challenge_url,
                            payload={"response": code},
                        )
                        if cr and cr.get("status") == "validated":
                            break

                # Poll workflow status
                for _ in range(5):
                    try:
                        payload = {
                            "sequence": 0,
                            "user_input": {"status": "continue"},
                        }
                        fr = request_post(
                            url=inquiries_url, payload=payload, json=True,
                        )
                        if fr:
                            tc = fr.get("type_context", {})
                            if tc.get("result") == "workflow_status_approved":
                                log.info("workflow_approved")
                                return
                            wf = fr.get("verification_workflow", {})
                            if wf.get(
                                "workflow_status"
                            ) == "workflow_status_approved":
                                log.info("workflow_approved")
                                return
                    except Exception:
                        pass
                    time.sleep(5)

            rh_auth._validate_sherrif_id = _patched_validate

            # Run rh.login() in a thread (it's synchronous)
            result = await _run_sync(
                rh.login,
                username=self._username,
                password=self._password,
                store_session=True,
            )

            if result:
                self._connected = True
                self._login_failed = False  # Reset circuit breaker
                log.info("telegram_mfa_login_success")

                # Upload session to GCS for persistence
                upload_rh_session_to_gcs()

                return True
            else:
                log.error("telegram_mfa_login_failed")
                return False

        except Exception as e:
            log.exception("telegram_mfa_login_error", error=str(e))
            return False
        finally:
            builtins.input = original_input
            # Restore original robin_stocks function
            try:
                rh_auth._validate_sherrif_id = original_validate
            except Exception:
                pass

    async def disconnect(self) -> None:
        """Logout from Robinhood."""
        try:
            await _run_sync(rh.logout)
            self._connected = False
            logger.info("robinhood_disconnected")
        except Exception as e:
            logger.exception("robinhood_disconnect_error", error=str(e))

    async def is_connected(self) -> bool:
        """Check if connected to Robinhood."""
        if not self._connected:
            return False

        try:
            # Try to get account info to verify session
            account = await _run_sync(rh.profiles.load_account_profile)
            return account is not None
        except Exception:
            self._connected = False
            return False

    async def _ensure_connected(self) -> bool:
        """Ensure we're connected, attempt reconnection if needed."""
        if await self.is_connected():
            return True
        return await self.connect()

    # =========================================================================
    # Account & Positions
    # =========================================================================

    async def get_account_info(self) -> AccountInfo | None:
        """Get account summary information."""
        if not await self._ensure_connected():
            return None

        try:
            portfolio = await _run_sync(rh.profiles.load_portfolio_profile)
            account = await _run_sync(rh.profiles.load_account_profile)

            if not portfolio:
                return None

            return AccountInfo(
                portfolio_value=float(portfolio.get("equity", 0)),
                cash_balance=float(portfolio.get("withdrawable_amount", 0)),
                buying_power=float(account.get("buying_power", 0)) if account else 0,
                positions_count=int(portfolio.get("open_positions", 0)),
            )

        except Exception as e:
            logger.exception("get_account_info_failed", error=str(e))
            return None

    async def get_positions(self) -> list[Position]:
        """Get all current positions."""
        if not await self._ensure_connected():
            return []

        try:
            raw_positions = await _run_sync(rh.account.get_open_stock_positions)

            if not raw_positions:
                return []

            positions = []
            total_value = 0.0

            for pos in raw_positions:
                try:
                    # Get symbol from instrument
                    instrument_url = pos.get("instrument")
                    instrument = await _run_sync(rh.stocks.get_instrument_by_url, instrument_url)
                    symbol = instrument.get("symbol", "UNKNOWN") if instrument else "UNKNOWN"

                    # Parse position data
                    quantity = float(pos.get("quantity", 0))
                    avg_cost = float(pos.get("average_buy_price", 0))

                    # Get current price
                    quote = await _run_sync(rh.stocks.get_latest_price, symbol)
                    current_price = float(quote[0]) if quote and quote[0] else avg_cost

                    # Calculate values
                    market_value = quantity * current_price
                    cost_basis = quantity * avg_cost
                    unrealized_pnl = market_value - cost_basis
                    unrealized_pnl_pct = (unrealized_pnl / cost_basis) if cost_basis > 0 else 0

                    positions.append(Position(
                        symbol=symbol,
                        quantity=quantity,
                        average_cost=avg_cost,
                        current_price=current_price,
                        market_value=market_value,
                        weight=0,  # Calculate after getting total
                        unrealized_pnl=unrealized_pnl,
                        unrealized_pnl_pct=unrealized_pnl_pct,
                    ))

                    total_value += market_value

                except Exception as e:
                    logger.warning("position_parse_error", error=str(e))
                    continue

            # Calculate weights
            if total_value > 0:
                for pos in positions:
                    pos.weight = pos.market_value / total_value

            # Sort by market value
            positions.sort(key=lambda p: p.market_value, reverse=True)

            return positions

        except Exception as e:
            logger.exception("get_positions_failed", error=str(e))
            return []

    async def get_position(self, symbol: str) -> Position | None:
        """Get position for a specific symbol."""
        positions = await self.get_positions()
        for pos in positions:
            if pos.symbol.upper() == symbol.upper():
                return pos
        return None

    # =========================================================================
    # Market Data
    # =========================================================================

    async def get_quote(self, symbol: str) -> Quote | None:
        """Get current quote for a symbol."""
        if not await self._ensure_connected():
            return None

        try:
            quote_data = await _run_sync(rh.stocks.get_quotes, symbol)

            if not quote_data or not quote_data[0]:
                return None

            q = quote_data[0]

            return Quote(
                symbol=symbol.upper(),
                bid=float(q.get("bid_price")) if q.get("bid_price") else None,
                ask=float(q.get("ask_price")) if q.get("ask_price") else None,
                last=float(q.get("last_trade_price", 0)),
                timestamp=datetime.now(timezone.utc),
            )

        except Exception as e:
            logger.exception("get_quote_failed", symbol=symbol, error=str(e))
            return None

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes for multiple symbols."""
        quotes = {}
        for symbol in symbols:
            quote = await self.get_quote(symbol)
            if quote:
                quotes[symbol.upper()] = quote
        return quotes

    # =========================================================================
    # Order Management
    # =========================================================================

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order."""
        log = logger.bind(
            symbol=request.symbol,
            side=request.side.value,
            quantity=request.quantity,
            notional=request.notional,
            order_type=request.order_type.value,
        )

        if not await self._ensure_connected():
            return OrderResult(
                success=False,
                status=OrderStatus.FAILED,
                error="Not connected to Robinhood",
            )

        log.info("placing_order")

        try:
            # Determine quantity if only notional provided
            quantity = request.quantity
            if quantity is None and request.notional:
                quote = await self.get_quote(request.symbol)
                if not quote:
                    return OrderResult(
                        success=False,
                        status=OrderStatus.FAILED,
                        error=f"Unable to get quote for {request.symbol}",
                    )
                # Calculate shares from notional
                price = quote.last
                quantity = request.notional / price
                log.info("calculated_quantity", price=price, quantity=quantity)

            # Map time in force
            tif_map = {
                TimeInForce.DAY: "gfd",
                TimeInForce.GTC: "gtc",
                TimeInForce.IOC: "ioc",
                TimeInForce.FOK: "fok",
            }

            # Place the order
            if request.side == OrderSide.BUY:
                if request.order_type == OrderType.MARKET:
                    result = await _run_sync(
                        rh.orders.order_buy_market,
                        symbol=request.symbol,
                        quantity=quantity,
                        timeInForce=tif_map.get(request.time_in_force, "gfd"),
                    )
                else:
                    result = await _run_sync(
                        rh.orders.order_buy_limit,
                        symbol=request.symbol,
                        quantity=quantity,
                        limitPrice=request.limit_price,
                        timeInForce=tif_map.get(request.time_in_force, "gfd"),
                    )
            else:  # SELL
                if request.order_type == OrderType.MARKET:
                    result = await _run_sync(
                        rh.orders.order_sell_market,
                        symbol=request.symbol,
                        quantity=quantity,
                        timeInForce=tif_map.get(request.time_in_force, "gfd"),
                    )
                else:
                    result = await _run_sync(
                        rh.orders.order_sell_limit,
                        symbol=request.symbol,
                        quantity=quantity,
                        limitPrice=request.limit_price,
                        timeInForce=tif_map.get(request.time_in_force, "gfd"),
                    )

            # Parse result
            if result and "id" in result:
                order_id = result.get("id")
                state = result.get("state", "").lower()

                status = OrderStatus.SUBMITTED
                if state == "filled":
                    status = OrderStatus.FILLED
                elif state == "partially_filled":
                    status = OrderStatus.PARTIAL
                elif state == "cancelled":
                    status = OrderStatus.CANCELLED
                elif state == "rejected":
                    status = OrderStatus.REJECTED
                elif state == "failed":
                    status = OrderStatus.FAILED

                filled_qty = float(result.get("cumulative_quantity", 0))
                avg_price = float(result.get("average_price", 0)) if result.get("average_price") else None

                log.info(
                    "order_placed",
                    order_id=order_id,
                    status=status.value,
                    state=state,
                )

                return OrderResult(
                    success=True,
                    order_id=order_id,
                    client_order_id=request.client_order_id,
                    status=status,
                    filled_quantity=filled_qty,
                    filled_price=avg_price,
                    filled_notional=filled_qty * avg_price if avg_price else 0,
                    broker_response=result,
                )

            else:
                error_msg = "Unknown error"
                if result and "detail" in result:
                    error_msg = result.get("detail")
                elif result and "non_field_errors" in result:
                    error_msg = result.get("non_field_errors")[0]

                log.error("order_failed", error=error_msg, result=result)

                return OrderResult(
                    success=False,
                    status=OrderStatus.REJECTED,
                    error=error_msg,
                    broker_response=result,
                )

        except Exception as e:
            log.exception("order_exception", error=str(e))
            return OrderResult(
                success=False,
                status=OrderStatus.FAILED,
                error=str(e),
            )

    async def get_order_status(self, order_id: str) -> OrderResult | None:
        """Get the current status of an order."""
        if not await self._ensure_connected():
            return None

        try:
            result = await _run_sync(rh.orders.get_stock_order_info, order_id)

            if not result:
                return None

            state = result.get("state", "").lower()

            status = OrderStatus.PENDING
            if state == "filled":
                status = OrderStatus.FILLED
            elif state == "partially_filled":
                status = OrderStatus.PARTIAL
            elif state == "cancelled":
                status = OrderStatus.CANCELLED
            elif state == "rejected":
                status = OrderStatus.REJECTED
            elif state == "failed":
                status = OrderStatus.FAILED
            elif state in ("queued", "confirmed", "pending"):
                status = OrderStatus.SUBMITTED

            filled_qty = float(result.get("cumulative_quantity", 0))
            avg_price = float(result.get("average_price", 0)) if result.get("average_price") else None

            return OrderResult(
                success=status in (OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.SUBMITTED),
                order_id=order_id,
                status=status,
                filled_quantity=filled_qty,
                filled_price=avg_price,
                filled_notional=filled_qty * avg_price if avg_price else 0,
                broker_response=result,
            )

        except Exception as e:
            logger.exception("get_order_status_failed", order_id=order_id, error=str(e))
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if not await self._ensure_connected():
            return False

        try:
            result = await _run_sync(rh.orders.cancel_stock_order, order_id)
            return result is not None

        except Exception as e:
            logger.exception("cancel_order_failed", order_id=order_id, error=str(e))
            return False

    async def get_open_orders(self) -> list[OrderResult]:
        """Get all open/pending orders."""
        if not await self._ensure_connected():
            return []

        try:
            orders = await _run_sync(rh.orders.get_all_open_stock_orders)

            if not orders:
                return []

            results = []
            for order in orders:
                order_id = order.get("id")
                state = order.get("state", "").lower()

                status = OrderStatus.PENDING
                if state in ("queued", "confirmed"):
                    status = OrderStatus.SUBMITTED
                elif state == "partially_filled":
                    status = OrderStatus.PARTIAL

                filled_qty = float(order.get("cumulative_quantity", 0))
                avg_price = float(order.get("average_price", 0)) if order.get("average_price") else None

                results.append(OrderResult(
                    success=True,
                    order_id=order_id,
                    status=status,
                    filled_quantity=filled_qty,
                    filled_price=avg_price,
                    filled_notional=filled_qty * avg_price if avg_price else 0,
                    broker_response=order,
                ))

            return results

        except Exception as e:
            logger.exception("get_open_orders_failed", error=str(e))
            return []


# Singleton instance
_adapter: RobinhoodAdapter | None = None


def get_robinhood_adapter() -> RobinhoodAdapter:
    """Get the Robinhood adapter singleton."""
    global _adapter
    if _adapter is None:
        _adapter = RobinhoodAdapter()
    return _adapter
