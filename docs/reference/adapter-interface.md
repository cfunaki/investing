# Adapter Interface Reference

Technical specification for sleeve adapters.

## Overview

Adapters are the bridge between signal sources (sleeves) and the reconciliation engine. Each adapter:

1. Fetches data from its source
2. Transforms it to the standard format
3. Outputs to `data/processed/target_allocations.json`

## Base Adapter Class

Location: `src/adapters/base.py`

```python
from abc import ABC, abstractmethod
from typing import List, Dict, Any

class BaseAdapter(ABC):
    """Base class for all sleeve adapters."""

    @abstractmethod
    def get_sleeve_name(self) -> str:
        """Return the name of this sleeve."""
        pass

    @abstractmethod
    def fetch_targets(self) -> List[Dict[str, Any]]:
        """
        Fetch and return target allocations.

        Returns:
            List of target allocation dicts with keys:
            - symbol (str): Ticker symbol
            - weight (int): Position weight
            - side (str): "long" or "short"
            - entry_price (float, optional): Entry price
            - stop_loss (float, optional): Stop loss price
            - target_price (float, optional): Target price
        """
        pass

    def save_targets(self, targets: List[Dict[str, Any]]) -> str:
        """Save targets to standard location."""
        import json
        from pathlib import Path

        output = {
            "sleeve": self.get_sleeve_name(),
            "allocations": targets,
            "generated_at": datetime.now().isoformat(),
        }

        output_path = Path("data/processed/target_allocations.json")
        output_path.write_text(json.dumps(output, indent=2))
        return str(output_path)
```

## Target Allocation Schema

Each target allocation must have:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string | Yes | Ticker symbol (e.g., "AAPL") |
| `weight` | integer | Yes | Position weight (1-10 typical) |
| `side` | string | Yes | "long" or "short" |
| `entry_price` | float | No | Entry price for price adjustment |
| `stop_loss` | float | No | Stop loss price |
| `target_price` | float | No | Target price |
| `entry_date` | string | No | ISO date of entry |

### Example

```json
{
  "symbol": "EME",
  "weight": 3,
  "side": "long",
  "entry_price": 687.73,
  "stop_loss": 705.00,
  "target_price": 850.00,
  "entry_date": "2026-01-15"
}
```

## Output Format

Adapters must output to `data/processed/target_allocations.json`:

```json
{
  "sleeve": "bravos",
  "allocations": [
    {
      "symbol": "EME",
      "weight": 3,
      "side": "long"
    },
    {
      "symbol": "NEE",
      "weight": 5,
      "side": "long"
    }
  ],
  "total_weight": 62,
  "generated_at": "2026-03-18T10:30:00"
}
```

## Existing Adapters

### Bravos Web Adapter

Location: `src/adapters/bravos_web.py`

Fetches from Bravos Research website via browser automation.

**Data flow:**
1. TypeScript scraper (`scripts/scrape-active-trades.ts`) extracts raw data
2. Python normalizer (`src/parsing/normalize_ideas.py`) cleans data
3. Position deriver (`src/parsing/derive_positions.py`) creates allocations

**Session requirement:** Requires authenticated session in `data/sessions/bravos.json`

### HTTP Client Adapter

Location: `src/adapters/http_client.py`

Base utilities for HTTP-based adapters.

## Implementing a New Adapter

### Step 1: Create the Adapter Class

```python
# src/adapters/my_sleeve.py

from src.adapters.base import BaseAdapter

class MySleeveAdapter(BaseAdapter):
    def __init__(self, config: dict = None):
        self.config = config or {}

    def get_sleeve_name(self) -> str:
        return "my-sleeve"

    def fetch_targets(self) -> list[dict]:
        # Implement data fetching logic
        raw_data = self._fetch_raw_data()

        # Transform to standard format
        targets = []
        for item in raw_data:
            targets.append({
                "symbol": item["ticker"],
                "weight": item["allocation"],
                "side": "long",
                "entry_price": item.get("entry"),
            })

        return targets

    def _fetch_raw_data(self):
        # Your data fetching logic here
        pass
```

### Step 2: Add CLI Entry Point

```python
# At bottom of src/adapters/my_sleeve.py

if __name__ == "__main__":
    adapter = MySleeveAdapter()
    targets = adapter.fetch_targets()
    output_path = adapter.save_targets(targets)
    print(f"Saved {len(targets)} targets to {output_path}")
```

### Step 3: Test

```bash
.venv/bin/python -m src.adapters.my_sleeve
cat data/processed/target_allocations.json
```

## How Reconciliation Consumes Adapters

The reconciliation engine (`src/recon/reconcile.py`):

1. Loads `data/processed/target_allocations.json`
2. Loads `data/processed/robinhood_holdings.json`
3. Compares targets vs holdings
4. Generates delta actions

The reconciliation engine is **sleeve-agnostic** - it only cares about the standard target allocation format.

## Data Models

### Signal (for event-driven)

```python
@dataclass
class Signal:
    source: str           # "email", "webhook", "manual"
    sleeve: str           # "bravos", "my-sleeve"
    timestamp: datetime
    payload: dict         # Source-specific data
```

### PortfolioIntent

```python
@dataclass
class PortfolioIntent:
    sleeve: str
    targets: List[TargetAllocation]
    created_at: datetime
    status: str           # "pending", "approved", "rejected", "executed"
```

## Best Practices

1. **Idempotent fetches**: Running the adapter twice should produce the same result
2. **Error handling**: Handle network failures, auth expiry gracefully
3. **Logging**: Log fetch timestamps and record counts
4. **Data validation**: Validate symbols exist, weights are positive
5. **Session management**: Handle authentication separately from data fetching
6. **Rate limiting**: Respect source API limits
