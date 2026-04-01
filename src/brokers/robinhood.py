"""
Robinhood broker adapter implementation.

This adapter wraps the robin_stocks library to provide
a consistent interface for the execution layer.
"""

import asyncio
from datetime import datetime, timezone
from functools import partial
from typing import Any

import pyotp
import robin_stocks.robinhood as rh
import structlog

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

    @property
    def name(self) -> str:
        return "robinhood"

    # =========================================================================
    # Connection
    # =========================================================================

    async def connect(self) -> bool:
        """Authenticate with Robinhood."""
        log = logger.bind(broker="robinhood")
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
                log.info("robinhood_connected")
                return True
            else:
                log.error("robinhood_login_failed")
                return False

        except Exception as e:
            log.exception("robinhood_connection_error", error=str(e))
            return False

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
