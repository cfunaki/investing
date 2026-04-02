#!/usr/bin/env python3
"""
Initialize the Bravos sleeve ledger from current holdings.

This script bootstraps the virtual ledger (sleeve_positions table) by:
1. Reading current Bravos target weights from bravos_trades.json
2. Reading current Robinhood holdings
3. Calculating which holdings belong to Bravos (based on symbols in targets)
4. Creating ledger entries with current share counts and weights

Run this ONCE to set up the initial state, then the delta reconciler
will handle subsequent updates.

Usage:
    python scripts/init-bravos-ledger.py [--unit-size 500] [--dry-run]
"""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.repositories.sleeve_position_repository import sleeve_position_repository
from src.db.session import get_db_context
from src.reconciliation.delta_reconciler import parse_bravos_weights


BRAVOS_TRADES_PATH = Path("data/processed/bravos_trades.json")
ROBINHOOD_HOLDINGS_PATH = Path("data/reports/robinhood_holdings.csv")


def load_robinhood_holdings() -> dict[str, dict]:
    """Load current Robinhood holdings from CSV."""
    import csv

    holdings = {}
    if not ROBINHOOD_HOLDINGS_PATH.exists():
        print(f"WARNING: No Robinhood holdings file at {ROBINHOOD_HOLDINGS_PATH}")
        return holdings

    with open(ROBINHOOD_HOLDINGS_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("Symbol", row.get("symbol", "")).upper()
            if symbol and symbol != "AABAZZ":  # Skip placeholder
                holdings[symbol] = {
                    "symbol": symbol,
                    "shares": Decimal(row.get("Quantity", row.get("quantity", "0"))),
                    "value": Decimal(row.get("Value", row.get("value", "0")).replace(",", "").replace("$", "")),
                }

    return holdings


async def init_ledger(unit_size: Decimal, dry_run: bool):
    """Initialize the Bravos ledger."""
    print("=" * 60)
    print("Initialize Bravos Sleeve Ledger")
    print("=" * 60)
    print()

    # Load Bravos target weights
    if not BRAVOS_TRADES_PATH.exists():
        print(f"ERROR: No Bravos trades file at {BRAVOS_TRADES_PATH}")
        print("Run: npx tsx scripts/scrape-active-trades.ts")
        return

    with open(BRAVOS_TRADES_PATH) as f:
        bravos_data = json.load(f)

    weights = parse_bravos_weights(bravos_data)
    print(f"Bravos target symbols: {len(weights)}")
    for symbol, weight in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"  {symbol}: weight {weight} (${float(weight * unit_size):,.0f} target)")
    print()

    # Load Robinhood holdings
    holdings = load_robinhood_holdings()
    print(f"Robinhood holdings: {len(holdings)} positions")
    print()

    # Find overlap (symbols in both Bravos targets AND current holdings)
    bravos_holdings = {}
    for symbol in weights:
        if symbol in holdings:
            bravos_holdings[symbol] = {
                **holdings[symbol],
                "weight": weights[symbol],
            }

    print(f"Positions to initialize (in both Bravos targets and RH holdings): {len(bravos_holdings)}")
    for symbol, info in sorted(bravos_holdings.items()):
        print(f"  {symbol}: {info['shares']} shares, ${float(info['value']):,.0f}, weight {info['weight']}")
    print()

    # Symbols in Bravos but NOT in holdings (need to buy)
    missing = set(weights.keys()) - set(holdings.keys())
    if missing:
        print(f"Symbols in Bravos targets but NOT in holdings ({len(missing)}):")
        for symbol in sorted(missing):
            print(f"  {symbol}: weight {weights[symbol]} (need to buy ${float(weights[symbol] * unit_size):,.0f})")
        print()

    if dry_run:
        print("[DRY RUN] Would initialize ledger with above positions")
        return

    # Initialize database
    try:
        async with get_db_context() as db:
            # Get or create Bravos sleeve
            sleeve = await sleeve_repository.get_by_name(db, "bravos")
            if not sleeve:
                print("ERROR: Bravos sleeve not found in database")
                print("Run the schema migration first")
                return

            print(f"Bravos sleeve ID: {sleeve.id}")
            print(f"Unit size: ${float(sleeve.unit_size):,.0f}")
            print()

            # Check if ledger already has entries
            existing = await sleeve_position_repository.get_by_sleeve(db, sleeve.id)
            if existing:
                print(f"WARNING: Ledger already has {len(existing)} positions")
                response = input("Overwrite? (y/N): ")
                if response.lower() != "y":
                    print("Aborted")
                    return

                # Delete existing
                for pos in existing:
                    await sleeve_position_repository.delete_position(
                        db, sleeve.id, pos.symbol
                    )
                print(f"Deleted {len(existing)} existing positions")

            # Create positions for holdings that match Bravos targets
            created = 0
            for symbol, info in bravos_holdings.items():
                await sleeve_position_repository.create_position(
                    db,
                    sleeve_id=sleeve.id,
                    symbol=symbol,
                    shares=info["shares"],
                    weight=info["weight"],
                    cost_basis=info["value"],  # Use current value as cost basis
                )
                created += 1
                print(f"  Created: {symbol} - {info['shares']} shares, weight {info['weight']}")

            await db.commit()
            print()
            print(f"Initialized {created} positions in Bravos ledger")

    except Exception as e:
        print(f"ERROR: Database operation failed: {e}")
        print()
        print("Make sure the database is accessible and schema is up to date.")
        raise


def main():
    parser = argparse.ArgumentParser(description="Initialize Bravos sleeve ledger")
    parser.add_argument(
        "--unit-size",
        type=float,
        default=500.0,
        help="Dollar amount per weight unit (default: 500)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    asyncio.run(init_ledger(Decimal(str(args.unit_size)), args.dry_run))


if __name__ == "__main__":
    main()
