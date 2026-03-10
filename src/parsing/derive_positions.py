"""
Derive target positions from normalized Bravos ideas.
Aggregates open ideas into target allocation percentages.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import TypedDict, Optional
from collections import defaultdict


class TargetAllocation(TypedDict):
    symbol: str
    target_pct: float  # e.g., 0.05 for 5%
    side: str  # 'long' or 'short'
    idea_count: int  # number of ideas for this symbol
    last_updated: str
    source: str


PROCESSED_DATA_DIR = Path("data/processed")
RAW_DATA_DIR = Path("data/raw")


def load_active_trades() -> list[dict] | None:
    """Load active trades from the research page scrape (preferred source)"""
    active_path = RAW_DATA_DIR / "active-trades-latest.json"

    if not active_path.exists():
        return None

    with open(active_path) as f:
        data = json.load(f)

    trades = data.get("trades", [])
    if not trades:
        return None

    print(f"Found active trades file with {len(trades)} positions")
    print(f"Last updated: {data.get('lastUpdated', 'unknown')}")

    return trades


def derive_from_active_trades(trades: list[dict]) -> list[TargetAllocation]:
    """
    Derive allocations from active trades (preferred method).
    These are the current positions shown on /research/ with explicit weights.
    """
    total_weight = sum(t.get("weight", 0) for t in trades)

    allocations: list[TargetAllocation] = []

    for trade in trades:
        symbol = trade.get("symbol")
        weight = trade.get("weight", 0)
        action = trade.get("action", "Long")

        if not symbol or weight <= 0:
            continue

        target_pct = weight / total_weight if total_weight > 0 else 0

        allocations.append({
            "symbol": symbol,
            "target_pct": round(target_pct, 4),
            "side": "short" if action.lower() == "short" else "long",
            "idea_count": 1,
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
            "source": "bravos_active_trades",
            "weight": weight,  # Keep raw weight for reference
        })

    # Sort by weight descending
    allocations.sort(key=lambda x: x["target_pct"], reverse=True)

    return allocations


def load_normalized_ideas() -> list[dict]:
    """Load normalized ideas from processed data"""
    ideas_path = PROCESSED_DATA_DIR / "ideas.json"

    if not ideas_path.exists():
        print(f"No normalized ideas found at {ideas_path}")
        print("Run normalize_ideas.py first")
        return []

    with open(ideas_path) as f:
        data = json.load(f)

    return data.get("ideas", [])


def derive_allocations(ideas: list[dict]) -> list[TargetAllocation]:
    """
    Derive target allocations from open ideas.

    Strategy:
    - Only consider 'open' status ideas
    - Group by symbol
    - Sum relative weights for each symbol
    - Normalize if weights don't sum to expected total
    """

    # Filter to open ideas only
    open_ideas = [idea for idea in ideas if idea.get("status") == "open"]
    print(f"Found {len(open_ideas)} open ideas out of {len(ideas)} total")

    if not open_ideas:
        # If no explicit 'open' status, treat all as open
        print("No ideas with 'open' status - treating all as open")
        open_ideas = ideas

    # Group by symbol and sum weights
    symbol_data: dict[str, dict] = defaultdict(lambda: {
        "total_weight": 0.0,
        "idea_count": 0,
        "side": "long",  # default
        "last_date": "",
    })

    for idea in open_ideas:
        symbol = idea.get("symbol")
        if not symbol:
            continue

        weight = idea.get("relative_weight")
        if weight is not None:
            symbol_data[symbol]["total_weight"] += weight
        else:
            # If no weight specified, we'll handle this below
            symbol_data[symbol]["total_weight"] += 0

        symbol_data[symbol]["idea_count"] += 1

        # Track side (buy = long, sell = short)
        side = idea.get("side", "buy")
        if side == "sell":
            symbol_data[symbol]["side"] = "short"

        # Track most recent date
        date = idea.get("date", "")
        if date > symbol_data[symbol]["last_date"]:
            symbol_data[symbol]["last_date"] = date

    # Check if we got any weights
    total_weight = sum(d["total_weight"] for d in symbol_data.values())
    has_weights = total_weight > 0

    if not has_weights:
        # No weights found - assign equal weight to each symbol
        print("No allocation weights found in ideas - assigning equal weight")
        num_symbols = len(symbol_data)
        if num_symbols > 0:
            equal_weight = 1.0 / num_symbols
            for data in symbol_data.values():
                data["total_weight"] = equal_weight
            total_weight = 1.0

    # Build allocations list
    allocations: list[TargetAllocation] = []

    for symbol, data in symbol_data.items():
        allocations.append({
            "symbol": symbol,
            "target_pct": data["total_weight"],
            "side": data["side"],
            "idea_count": data["idea_count"],
            "last_updated": data["last_date"] or datetime.now().strftime("%Y-%m-%d"),
            "source": "bravos",
        })

    # Sort by weight descending
    allocations.sort(key=lambda x: x["target_pct"], reverse=True)

    return allocations


def validate_allocations(allocations: list[TargetAllocation]) -> dict:
    """Check allocation percentages and return validation info"""
    total_pct = sum(a["target_pct"] for a in allocations)

    return {
        "total_allocation_pct": total_pct,
        "implied_cash_pct": max(0, 1.0 - total_pct),
        "is_over_allocated": total_pct > 1.0,
        "symbol_count": len(allocations),
    }


def run_derivation() -> list[TargetAllocation]:
    """Main entry point: load ideas, derive allocations, save"""

    # First try to use active trades (preferred - has explicit weights)
    active_trades = load_active_trades()

    if active_trades:
        print("Using active trades from /research/ page (has explicit weights)")
        allocations = derive_from_active_trades(active_trades)
    else:
        # Fall back to normalized ideas (historical, no weights)
        print("No active trades found, falling back to normalized ideas")
        ideas = load_normalized_ideas()

        if not ideas:
            return []

        allocations = derive_allocations(ideas)
    print(f"Derived {len(allocations)} target allocations")

    # Validate
    validation = validate_allocations(allocations)
    print(f"Total allocation: {validation['total_allocation_pct']:.1%}")
    print(f"Implied cash position: {validation['implied_cash_pct']:.1%}")

    if validation["is_over_allocated"]:
        print("WARNING: Total allocation exceeds 100%!")

    # Save
    output_path = PROCESSED_DATA_DIR / "target_allocations.json"

    with open(output_path, "w") as f:
        json.dump({
            "allocations": allocations,
            "validation": validation,
            "derived_at": datetime.now().isoformat(),
        }, f, indent=2)

    print(f"Saved target allocations to {output_path}")

    # Print summary
    print("\nTarget Allocations:")
    print("-" * 50)
    for alloc in allocations:
        pct_str = f"{alloc['target_pct']:.1%}".rjust(6)
        side_str = "SHORT" if alloc["side"] == "short" else ""
        print(f"  {alloc['symbol'].ljust(6)} {pct_str}  {side_str}")
    print("-" * 50)

    return allocations


if __name__ == "__main__":
    run_derivation()
