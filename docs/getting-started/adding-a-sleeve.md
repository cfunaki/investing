# Adding a New Sleeve

This guide explains how to add a new signal source (sleeve) to the platform.

## Sleeve Architecture

Each sleeve needs:
1. **Data source**: Where target allocations come from
2. **Adapter**: Code to fetch and normalize the data
3. **Claude command** (optional): For easy invocation

## Data Model

Every sleeve must produce a list of `TargetAllocation`:

```python
@dataclass
class TargetAllocation:
    symbol: str          # Ticker symbol (e.g., "AAPL")
    weight: int          # Position size weight (e.g., 5)
    side: str            # "long" or "short"
    entry_price: float   # Optional: Entry price for price-adjustment
    stop_loss: float     # Optional: Stop loss price
    target_price: float  # Optional: Target price
```

The reconciliation engine converts weights to dollars:
```
Target Value = weight × dollars_per_weight
```

## Option 1: CSV-Based Sleeve (Simplest)

Create a CSV file with your allocations:

```csv
symbol,weight,side,entry_price,stop_loss,target_price
AAPL,5,long,175.00,165.00,200.00
MSFT,3,long,380.00,360.00,420.00
```

Save to `data/raw/my-sleeve.csv`

Create an adapter in `src/adapters/csv_sleeve.py`:

```python
import csv
from pathlib import Path
from src.adapters.base import BaseAdapter

class CSVSleeveAdapter(BaseAdapter):
    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)

    def get_sleeve_name(self) -> str:
        return self.csv_path.stem

    def fetch_targets(self) -> list[dict]:
        targets = []
        with open(self.csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                targets.append({
                    "symbol": row["symbol"],
                    "weight": int(row["weight"]),
                    "side": row.get("side", "long"),
                    "entry_price": float(row["entry_price"]) if row.get("entry_price") else None,
                    "stop_loss": float(row["stop_loss"]) if row.get("stop_loss") else None,
                    "target_price": float(row["target_price"]) if row.get("target_price") else None,
                })
        return targets
```

## Option 2: API-Based Sleeve

For sleeves that fetch from an API:

```python
import requests
from src.adapters.base import BaseAdapter

class APISleeveAdapter(BaseAdapter):
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key

    def get_sleeve_name(self) -> str:
        return "my-api-sleeve"

    def fetch_targets(self) -> list[dict]:
        response = requests.get(
            self.api_url,
            headers={"Authorization": f"Bearer {self.api_key}"}
        )
        data = response.json()

        # Transform API response to target allocations
        return [
            {
                "symbol": item["ticker"],
                "weight": item["allocation"],
                "side": "long",
            }
            for item in data["positions"]
        ]
```

## Option 3: Web Scraping Sleeve

For sleeves that scrape a website (like Bravos):

See `src/adapters/bravos_web.py` and `scripts/scrape-active-trades.ts` for a complete example.

Key components:
1. **Playwright script** (TypeScript): Navigate and extract data
2. **Session management**: Persist authentication cookies
3. **Python adapter**: Load scraped JSON and transform

## Creating a Claude Command

Create `.claude/commands/sync-my-sleeve.md`:

```markdown
---
description: "Sync portfolio with my custom sleeve"
allowed-tools: ["Bash", "Read", "TodoWrite"]
---

# Sync Portfolio with My Sleeve

## Execution Steps

1. **Load sleeve data**
   ```bash
   .venv/bin/python -m src.adapters.my_sleeve
   ```

2. **Run reconciliation**
   ```bash
   .venv/bin/python -m src.recon.reconcile --fixed-dollar 500
   ```

3. **Show proposed trades and confirm**

4. **Execute if confirmed**
   ```bash
   .venv/bin/python -m src.trading.execute_trades --live
   ```
```

## Registering with Reconciliation

To use your sleeve with the reconciliation engine, ensure your adapter outputs to `data/processed/target_allocations.json`:

```python
import json
from pathlib import Path

def save_targets(targets: list[dict], sleeve_name: str):
    output = {
        "sleeve": sleeve_name,
        "allocations": targets,
    }
    Path("data/processed/target_allocations.json").write_text(
        json.dumps(output, indent=2)
    )
```

Then run reconciliation:
```bash
.venv/bin/python -m src.recon.reconcile --fixed-dollar 500
```

## Testing Your Sleeve

1. **Fetch targets**:
   ```bash
   .venv/bin/python -m src.adapters.my_sleeve
   cat data/processed/target_allocations.json
   ```

2. **Dry-run reconciliation**:
   ```bash
   .venv/bin/python -m src.recon.reconcile --fixed-dollar 500
   ```

3. **Verify output**:
   ```bash
   cat data/processed/reconciliation.json
   ```

## Best Practices

1. **Idempotent fetches**: Running the adapter twice should produce the same result
2. **Error handling**: Handle network failures, auth expiry gracefully
3. **Logging**: Log fetch timestamps and record counts
4. **Data validation**: Validate symbols exist, weights are positive
