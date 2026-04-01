"""
Abstract broker adapter interface.

This module defines the interface that all broker adapters must implement.
The abstraction allows the system to work with multiple brokers in the future.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID


class OrderSide(str, Enum):
    """Order side."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Order type."""

    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    """Order execution status."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class TimeInForce(str, Enum):
    """Time in force for orders."""

    DAY = "day"  # Good for day
    GTC = "gtc"  # Good till cancelled
    IOC = "ioc"  # Immediate or cancel
    FOK = "fok"  # Fill or kill


@dataclass
class Position:
    """A current position in the portfolio."""

    symbol: str
    quantity: float
    average_cost: float
    current_price: float
    market_value: float
    weight: float  # As fraction of portfolio
    unrealized_pnl: float
    unrealized_pnl_pct: float


@dataclass
class AccountInfo:
    """Account summary information."""

    portfolio_value: float
    cash_balance: float
    buying_power: float
    positions_count: int


@dataclass
class OrderRequest:
    """Request to place an order."""

    symbol: str
    side: OrderSide
    quantity: float | None = None  # Shares (if known)
    notional: float | None = None  # Dollar amount (alternative to quantity)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.DAY

    # Idempotency
    client_order_id: str | None = None

    def __post_init__(self):
        """Validate order request."""
        if self.quantity is None and self.notional is None:
            raise ValueError("Either quantity or notional must be specified")

        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("Limit price required for limit orders")


@dataclass
class OrderResult:
    """Result of an order submission."""

    success: bool
    order_id: str | None = None
    client_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    filled_price: float | None = None
    filled_notional: float = 0.0
    error: str | None = None
    broker_response: dict[str, Any] | None = None
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Quote:
    """Current market quote for a symbol."""

    symbol: str
    bid: float | None
    ask: float | None
    last: float
    timestamp: datetime


class BrokerAdapter(ABC):
    """
    Abstract base class for broker adapters.

    All broker implementations must inherit from this class
    and implement the required methods.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the broker name."""
        ...

    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection/authentication with the broker.

        Returns:
            True if connected successfully
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker."""
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        """Check if connected to the broker."""
        ...

    # =========================================================================
    # Account & Positions
    # =========================================================================

    @abstractmethod
    async def get_account_info(self) -> AccountInfo | None:
        """Get account summary information."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get all current positions."""
        ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None:
        """Get position for a specific symbol."""
        ...

    # =========================================================================
    # Market Data
    # =========================================================================

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote | None:
        """Get current quote for a symbol."""
        ...

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes for multiple symbols."""
        ...

    # =========================================================================
    # Order Management
    # =========================================================================

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        """
        Place an order.

        Args:
            request: The order request

        Returns:
            OrderResult with status and details
        """
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderResult | None:
        """Get the current status of an order."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order.

        Returns:
            True if cancellation was successful
        """
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[OrderResult]:
        """Get all open/pending orders."""
        ...

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def check_order_exists(
        self,
        symbol: str,
        side: OrderSide,
        client_order_id: str | None = None,
    ) -> OrderResult | None:
        """
        Check if an order already exists (for idempotency).

        This is used to check for existing orders before retrying
        after a timeout or error.

        Args:
            symbol: The symbol
            side: Order side
            client_order_id: Optional client order ID to match

        Returns:
            Existing order if found, None otherwise
        """
        open_orders = await self.get_open_orders()

        for order in open_orders:
            # Match by client order ID if provided
            if client_order_id and order.client_order_id == client_order_id:
                return order

        return None

    def is_market_open(self) -> bool:
        """
        Check if the market is currently open.

        Returns:
            True if market is open for trading
        """
        now = datetime.now(timezone.utc)
        weekday = now.weekday()

        # Weekend check
        if weekday >= 5:  # Saturday = 5, Sunday = 6
            return False

        # Convert to Eastern time (approximate - doesn't handle DST perfectly)
        # Market hours: 9:30 AM - 4:00 PM ET
        # In UTC: 14:30 - 21:00 (EST) or 13:30 - 20:00 (EDT)

        hour = now.hour
        minute = now.minute

        # Approximate market hours in UTC (conservative)
        # Open: 13:30 UTC (9:30 AM EDT) / 14:30 UTC (9:30 AM EST)
        # Close: 20:00 UTC (4:00 PM EDT) / 21:00 UTC (4:00 PM EST)

        market_open_utc = 13 * 60 + 30  # 13:30 UTC (EDT summer)
        market_close_utc = 21 * 60  # 21:00 UTC (EST winter)

        current_minutes = hour * 60 + minute

        return market_open_utc <= current_minutes < market_close_utc
