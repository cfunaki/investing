"""
Base adapter interface for sleeve data sources.

Each sleeve has an adapter responsible for:
1. Detecting when new data is available (signals)
2. Fetching the current portfolio state
3. Normalizing data into the canonical format
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass
class Allocation:
    """
    A single portfolio allocation from a sleeve.

    This is the normalized format that all adapters produce.
    """

    symbol: str
    target_weight: float  # 0.0 to 1.0
    side: str  # 'long' or 'short'

    # Optional metadata
    raw_weight: int | None = None  # Original weight if applicable
    asset_name: str | None = None  # Full name of the asset
    category: str | None = None  # Category grouping if provided


@dataclass
class PortfolioSnapshot:
    """
    A point-in-time snapshot of a sleeve's target portfolio.
    """

    sleeve_name: str
    allocations: list[Allocation]
    scraped_at: datetime
    last_updated: datetime | None  # When the source data was last updated
    total_positions: int

    # Metadata
    raw_data: dict[str, Any] | None = None  # Original data for debugging
    latency_ms: int = 0
    cold_start: bool = False


@dataclass
class AdapterError:
    """
    Error from an adapter operation.
    """

    error_type: str  # 'auth', 'parse', 'timeout', 'network', 'unknown'
    message: str
    recoverable: bool = True  # Can we retry?
    raw_error: Exception | None = None


class SleeveAdapter(ABC):
    """
    Abstract base class for sleeve data adapters.

    Each sleeve (Bravos, future newsletters, etc.) implements this interface.
    """

    @property
    @abstractmethod
    def sleeve_name(self) -> str:
        """Return the canonical name of this sleeve."""
        pass

    @property
    @abstractmethod
    def adapter_type(self) -> str:
        """Return the adapter type identifier."""
        pass

    @abstractmethod
    async def fetch_portfolio(
        self, force_refresh: bool = False
    ) -> PortfolioSnapshot | AdapterError:
        """
        Fetch the current portfolio state from the source.

        Args:
            force_refresh: Force a fresh fetch even if cached data exists

        Returns:
            PortfolioSnapshot on success, AdapterError on failure
        """
        pass

    @abstractmethod
    async def check_health(self) -> dict[str, Any]:
        """
        Check if the adapter is healthy and can fetch data.

        Returns:
            Health status dict with at least 'healthy' boolean
        """
        pass

    async def close(self):
        """Clean up adapter resources. Override if needed."""
        pass


class AdapterRegistry:
    """
    Registry for sleeve adapters.

    Allows looking up adapters by sleeve name or adapter type.
    """

    def __init__(self):
        self._adapters: dict[str, SleeveAdapter] = {}
        self._by_type: dict[str, type[SleeveAdapter]] = {}

    def register(self, adapter_class: type[SleeveAdapter]):
        """
        Register an adapter class.

        The adapter will be instantiated lazily when first accessed.
        """
        # Create a temporary instance to get metadata
        # In practice, we'd want a better way to get class-level properties
        self._by_type[adapter_class.__name__] = adapter_class

    def get(self, sleeve_name: str) -> SleeveAdapter | None:
        """
        Get an adapter instance by sleeve name.

        Returns None if no adapter is registered for this sleeve.
        """
        return self._adapters.get(sleeve_name)

    def register_instance(self, adapter: SleeveAdapter):
        """
        Register a specific adapter instance.
        """
        self._adapters[adapter.sleeve_name] = adapter

    def list_sleeves(self) -> list[str]:
        """Return list of registered sleeve names."""
        return list(self._adapters.keys())

    async def close_all(self):
        """Close all adapter instances."""
        for adapter in self._adapters.values():
            await adapter.close()
        self._adapters.clear()


# Global registry instance
adapter_registry = AdapterRegistry()
