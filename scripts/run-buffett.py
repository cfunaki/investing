#!/usr/bin/env python3
"""
Buffett sleeve pipeline.
Fetches Berkshire 13F from SEC EDGAR, calculates allocations, and shows proposed portfolio.

Usage:
    python scripts/run-buffett.py fetch     # Fetch and display 13F holdings
    python scripts/run-buffett.py allocate  # Calculate target allocations
    python scripts/run-buffett.py reconcile # Compare to current holdings (dry-run)
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.adapters.buffett_13f import Buffett13FAdapter


async def cmd_fetch():
    """Fetch and display the latest Berkshire 13F filing."""
    print("=" * 60)
    print("Buffett Sleeve - Fetch 13F")
    print("=" * 60)
    print()

    adapter = Buffett13FAdapter()

    print("Fetching latest 13F from SEC EDGAR...")
    print()

    result = await adapter.fetch_portfolio()

    if hasattr(result, "error_type"):
        print(f"ERROR: {result.message}")
        return 1

    # Get the cached filing for detailed view
    filing = adapter.get_cached_filing()
    if not filing:
        print("No filing data available")
        return 1

    print(f"Filer: {filing.filer_name}")
    print(f"CIK: {filing.cik}")
    print(f"Report Period: {filing.report_date}")
    print(f"Filed Date: {filing.filed_date}")
    print(f"Accession: {filing.accession_number}")
    print()
    print(f"Total Portfolio Value: ${filing.total_value:,.0f}")
    print(f"Total Positions: {filing.position_count}")
    print()

    print("Top 15 Holdings (by value):")
    print("-" * 70)
    print(f"{'#':<3} {'Ticker':<8} {'Issuer':<25} {'Value':>15} {'Weight':>8}")
    print("-" * 70)

    sorted_holdings = sorted(filing.holdings, key=lambda h: h.value, reverse=True)
    for i, h in enumerate(sorted_holdings[:15], 1):
        ticker = h.ticker or "???"
        issuer = h.issuer_name[:24]
        value = f"${h.value:,.0f}"
        weight = f"{h.weight_pct:.1f}%" if h.weight_pct else "N/A"
        print(f"{i:<3} {ticker:<8} {issuer:<25} {value:>15} {weight:>8}")

    print()
    return 0


async def cmd_allocate():
    """Calculate target allocations from 13F."""
    print("=" * 60)
    print("Buffett Sleeve - Calculate Allocations")
    print("=" * 60)
    print()

    adapter = Buffett13FAdapter()

    print("Fetching and processing 13F...")
    print()

    result = await adapter.fetch_portfolio()

    if hasattr(result, "error_type"):
        print(f"ERROR: {result.message}")
        return 1

    print(f"Report Date: {result.last_updated}")
    print(f"Positions Selected: {result.total_positions}")
    print()

    # Display allocations
    print("Target Allocations:")
    print("-" * 60)
    print(f"{'#':<3} {'Symbol':<8} {'Weight':>8} {'Target $':>12} {'Issuer':<25}")
    print("-" * 60)

    dollars_per_weight = adapter.config.get("dollars_per_weight", 500)
    total_weight = sum(a.raw_weight or 0 for a in result.allocations)
    total_target = 0

    for i, a in enumerate(result.allocations, 1):
        raw_weight = a.raw_weight or 0
        target_dollars = raw_weight * dollars_per_weight
        total_target += target_dollars
        issuer = (a.asset_name or "")[:24]
        print(
            f"{i:<3} {a.symbol:<8} {raw_weight:>8} ${target_dollars:>10,.0f} {issuer:<25}"
        )

    print("-" * 60)
    print(f"{'Total':<12} {total_weight:>8} ${total_target:>10,.0f}")
    print()

    # Save allocations to JSON
    output_path = Path("data/processed/buffett_allocations.json")
    output_data = {
        "sleeve": "buffett",
        "generated_at": datetime.now().isoformat(),
        "report_date": str(result.last_updated.date()) if result.last_updated else None,
        "dollars_per_weight": dollars_per_weight,
        "total_weight": total_weight,
        "total_target_value": total_target,
        "allocations": [
            {
                "symbol": a.symbol,
                "raw_weight": a.raw_weight,
                "target_pct": a.target_weight,
                "target_value": (a.raw_weight or 0) * dollars_per_weight,
                "side": a.side,
                "issuer": a.asset_name,
            }
            for a in result.allocations
        ],
    }

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"Saved allocations to: {output_path}")
    print()

    return 0


async def cmd_reconcile():
    """Compare target allocations to current holdings and save reconciliation."""
    print("=" * 60)
    print("Buffett Sleeve - Reconcile")
    print("=" * 60)
    print()

    # Check if we have buffett allocations
    alloc_path = Path("data/processed/buffett_allocations.json")
    if not alloc_path.exists():
        print("No allocations file found. Run 'allocate' first.")
        print()
        print("Running allocate now...")
        await cmd_allocate()
        print()

    # Check if we have robinhood holdings
    holdings_path = Path("data/processed/robinhood_holdings.json")
    if not holdings_path.exists():
        print("No Robinhood holdings found.")
        print("Run 'make holdings' or fetch holdings first.")
        return 1

    # Load data
    with open(alloc_path) as f:
        allocations = json.load(f)

    with open(holdings_path) as f:
        holdings_data = json.load(f)

    # Build holdings lookup
    current_holdings = {}
    for h in holdings_data.get("holdings", []):
        symbol = h.get("symbol")
        if symbol:
            current_holdings[symbol] = {
                "quantity": float(h.get("quantity", 0)),
                "market_value": float(h.get("market_value", 0)),
                "price": float(h.get("current_price", 0)),
            }

    # Calculate deltas (only for Buffett sleeve positions)
    print("Reconciliation (Buffett Sleeve):")
    print("-" * 75)
    print(f"{'Symbol':<8} {'Target $':>12} {'Current $':>12} {'Delta $':>12} {'Action':<10}")
    print("-" * 75)

    deltas = []
    for alloc in allocations.get("allocations", []):
        symbol = alloc["symbol"]
        target_value = alloc["target_value"]
        current = current_holdings.get(symbol, {})
        current_value = current.get("market_value", 0)
        current_price = current.get("price", 0)
        delta = target_value - current_value

        if abs(delta) < 50:  # $50 threshold
            action = "hold"
        elif delta > 0:
            action = "enter" if current_value == 0 else "buy"
        else:
            action = "sell"

        deltas.append({
            "symbol": symbol,
            "target_value": target_value,
            "current_value": current_value,
            "suggested_trade_value": delta,
            "action": action,
            "current_price": current_price,
            "notes": f"Buffett sleeve: {alloc.get('issuer', '')}",
        })

        action_display = action.upper()
        print(
            f"{symbol:<8} ${target_value:>10,.0f} ${current_value:>10,.0f} "
            f"${delta:>+10,.0f} {action_display:<10}"
        )

    print("-" * 75)
    print()

    total_buy = sum(d["suggested_trade_value"] for d in deltas if d["suggested_trade_value"] > 0)
    total_sell = sum(abs(d["suggested_trade_value"]) for d in deltas if d["suggested_trade_value"] < 0)

    print(f"Total to Buy:  ${total_buy:,.0f}")
    print(f"Total to Sell: ${total_sell:,.0f}")
    print(f"Net Cash Flow: ${total_sell - total_buy:,.0f}")
    print()

    # Save reconciliation in format compatible with execute_trades.py
    recon_output = {
        "sleeve": "buffett",
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_buy": total_buy,
            "total_sell": total_sell,
            "net_cash_flow": total_sell - total_buy,
            "position_count": len(deltas),
        },
        "deltas": deltas,
    }

    output_path = Path("data/processed/buffett_reconciliation.json")
    with open(output_path, "w") as f:
        json.dump(recon_output, f, indent=2)

    print(f"Saved reconciliation to: {output_path}")
    print()
    print("To execute trades, run: python scripts/run-buffett.py execute")
    print()

    return 0


def cmd_execute(dry_run: bool = False, no_confirm: bool = False):
    """Execute Buffett sleeve trades."""
    import time
    from src.robinhood.auth import ensure_logged_in

    print("=" * 60)
    print(f"Buffett Sleeve - Execute {'(DRY RUN)' if dry_run else '(LIVE)'}")
    print("=" * 60)
    print()

    # Load reconciliation
    recon_path = Path("data/processed/buffett_reconciliation.json")
    if not recon_path.exists():
        print("No reconciliation file found. Run 'reconcile' first.")
        return 1

    with open(recon_path) as f:
        recon = json.load(f)

    deltas = recon.get("deltas", [])

    # Filter to actionable trades
    buys = [d for d in deltas if d["action"] in ("enter", "buy") and d["suggested_trade_value"] >= 1]
    sells = [d for d in deltas if d["action"] == "sell" and abs(d["suggested_trade_value"]) >= 1]

    if not buys and not sells:
        print("No trades to execute.")
        return 0

    print(f"Trades to execute:")
    print(f"  Buys:  {len(buys)}")
    print(f"  Sells: {len(sells)}")
    print()

    if not dry_run:
        if not no_confirm:
            confirm = input("Type 'EXECUTE' to confirm live trades: ")
            if confirm != "EXECUTE":
                print("Aborted.")
                return 1
        print()
        print("Logging into Robinhood...")
        ensure_logged_in()

    # Import execution functions
    from src.trading.execute_trades import execute_buy, execute_sell, log_trade

    results = {
        "sells": [],
        "buys": [],
        "errors": [],
        "executed_at": datetime.now().isoformat(),
        "dry_run": dry_run,
    }

    # Execute sells first (if any)
    if sells:
        print(f"\n{'='*50}")
        print(f"EXECUTING SELLS ({len(sells)} orders)")
        print(f"{'='*50}")

        for i, trade in enumerate(sells, 1):
            symbol = trade["symbol"]
            amount = abs(trade["suggested_trade_value"])

            print(f"\n  [{i}/{len(sells)}] SELL {symbol}")
            print(f"      Amount: ${amount:,.2f}")

            if not dry_run and not no_confirm:
                response = input("      Execute? (y/n/q): ").strip().lower()
                if response == 'q':
                    print("      Aborting remaining trades.")
                    break
                if response != 'y':
                    print("      Skipped.")
                    continue

            # For sells, we need quantity
            price = trade.get("current_price", 0)
            if price > 0:
                quantity = amount / price
            else:
                print(f"      ERROR: No price available")
                continue

            result = execute_sell(symbol, quantity, amount_dollars=amount, dry_run=dry_run)
            result["symbol"] = symbol
            result["intended_value"] = amount

            if result.get("state") in ("queued", "unconfirmed", "confirmed", "filled") or result.get("status") == "dry_run":
                print(f"      OK - Order {'simulated' if dry_run else result.get('state', 'placed')}")
                results["sells"].append(result)
                if not dry_run:
                    log_trade({"type": "sell", "sleeve": "buffett", **result})
            else:
                print(f"      ERROR: {result}")
                results["errors"].append(result)

            if not dry_run:
                time.sleep(2)

    # Execute buys
    if buys:
        print(f"\n{'='*50}")
        print(f"EXECUTING BUYS ({len(buys)} orders)")
        print(f"{'='*50}")

        for i, trade in enumerate(buys, 1):
            symbol = trade["symbol"]
            amount = trade["suggested_trade_value"]

            print(f"\n  [{i}/{len(buys)}] BUY {symbol}")
            print(f"      Amount: ${amount:,.2f}")

            if not dry_run and not no_confirm:
                response = input("      Execute? (y/n/q): ").strip().lower()
                if response == 'q':
                    print("      Aborting remaining trades.")
                    break
                if response != 'y':
                    print("      Skipped.")
                    continue

            result = execute_buy(symbol, amount, dry_run=dry_run)
            result["symbol"] = symbol
            result["intended_amount"] = amount

            if result.get("state") in ("queued", "unconfirmed", "confirmed", "filled") or result.get("status") == "dry_run":
                state_msg = "simulated" if dry_run else result.get("state", "placed")
                print(f"      OK - Order {state_msg}")
                results["buys"].append(result)
                if not dry_run:
                    log_trade({"type": "buy", "sleeve": "buffett", **result})
            else:
                print(f"      ERROR: {result}")
                results["errors"].append(result)

            if not dry_run:
                time.sleep(2)

    # Summary
    print(f"\n{'='*50}")
    print("EXECUTION SUMMARY")
    print(f"{'='*50}")
    print(f"  Sells executed: {len(results['sells'])}")
    print(f"  Buys executed:  {len(results['buys'])}")
    print(f"  Errors:         {len(results['errors'])}")
    print(f"  Mode:           {'DRY RUN' if dry_run else 'LIVE'}")
    print()

    return 0


async def main():
    parser = argparse.ArgumentParser(
        description="Buffett sleeve management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  fetch      Fetch and display latest Berkshire 13F
  allocate   Calculate target allocations from 13F
  reconcile  Compare to current holdings and save reconciliation
  execute    Execute trades (use --dry-run for simulation)
        """,
    )
    parser.add_argument(
        "command",
        choices=["fetch", "allocate", "reconcile", "execute"],
        help="Command to run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate trades without executing (for execute command)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip per-trade confirmation prompts (for execute command)",
    )

    args = parser.parse_args()

    if args.command == "fetch":
        return await cmd_fetch()
    elif args.command == "allocate":
        return await cmd_allocate()
    elif args.command == "reconcile":
        return await cmd_reconcile()
    elif args.command == "execute":
        return cmd_execute(dry_run=args.dry_run, no_confirm=args.no_confirm)


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code or 0)
