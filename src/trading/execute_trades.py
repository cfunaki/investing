"""
Execute trades on Robinhood based on reconciliation results.
Supports review-then-execute flow for safety.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import robin_stocks.robinhood as rh

from src.robinhood.auth import login, ensure_logged_in


PROCESSED_DATA_DIR = Path("data/processed")
TRADE_LOG_DIR = Path("data/trades")


def load_reconciliation() -> dict:
    """Load the latest reconciliation results"""
    recon_path = PROCESSED_DATA_DIR / "reconciliation.json"
    if not recon_path.exists():
        raise FileNotFoundError("No reconciliation.json found. Run reconciliation first.")

    with open(recon_path) as f:
        return json.load(f)


def load_holdings() -> dict:
    """Load current holdings for quantity lookup"""
    holdings_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"
    if not holdings_path.exists():
        raise FileNotFoundError("No holdings data found.")

    with open(holdings_path) as f:
        return json.load(f)


def get_pending_orders() -> list[dict]:
    """Get list of pending/queued orders"""
    try:
        orders = rh.get_all_open_stock_orders()
        return orders if orders else []
    except Exception as e:
        print(f"Warning: Could not fetch pending orders: {e}")
        return []


def execute_sell(symbol: str, quantity: float, dry_run: bool = False) -> dict:
    """
    Execute a sell order.

    Args:
        symbol: Stock ticker
        quantity: Number of shares to sell
        dry_run: If True, don't actually execute

    Returns:
        Order result dict
    """
    if dry_run:
        return {"status": "dry_run", "symbol": symbol, "quantity": quantity, "side": "sell"}

    try:
        # Use fractional sell for precision
        result = rh.order_sell_fractional_by_quantity(
            symbol=symbol,
            quantity=quantity,
            timeInForce='gfd',  # Good for day
        )
        return result
    except Exception as e:
        return {"status": "error", "error": str(e), "symbol": symbol}


def execute_buy(symbol: str, amount_dollars: float, dry_run: bool = False) -> dict:
    """
    Execute a buy order by dollar amount.

    Args:
        symbol: Stock ticker
        amount_dollars: Dollar amount to invest
        dry_run: If True, don't actually execute

    Returns:
        Order result dict
    """
    if dry_run:
        return {"status": "dry_run", "symbol": symbol, "amount": amount_dollars, "side": "buy"}

    try:
        # Robinhood requires minimum $1 for fractional orders
        if amount_dollars < 1:
            return {"status": "skipped", "reason": "Amount too small (<$1)", "symbol": symbol}

        result = rh.order_buy_fractional_by_price(
            symbol=symbol,
            amountInDollars=amount_dollars,
            timeInForce='gfd',  # Good for day
        )
        return result
    except Exception as e:
        return {"status": "error", "error": str(e), "symbol": symbol}


def log_trade(trade: dict):
    """Log trade to file for record keeping"""
    TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = TRADE_LOG_DIR / f"trades-{datetime.now().strftime('%Y%m%d')}.json"

    trades = []
    if log_file.exists():
        with open(log_file) as f:
            trades = json.load(f)

    trade["logged_at"] = datetime.now().isoformat()
    trades.append(trade)

    with open(log_file, "w") as f:
        json.dump(trades, f, indent=2)


def execute_all_trades(dry_run: bool = False, confirm_each: bool = True) -> dict:
    """
    Execute all trades from reconciliation.
    Sells first to free up cash, then buys.

    Args:
        dry_run: If True, simulate without executing
        confirm_each: If True, pause after each trade for confirmation

    Returns:
        Summary of executed trades
    """
    # Login to Robinhood
    print("Logging into Robinhood...")
    ensure_logged_in()

    # Load data
    recon = load_reconciliation()
    holdings_data = load_holdings()
    holdings_map = {h["symbol"]: h for h in holdings_data.get("holdings", [])}

    deltas = recon.get("deltas", [])

    # Separate sells and buys
    sells = [d for d in deltas if d["action"] in ("exit", "sell")]
    buys = [d for d in deltas if d["action"] in ("enter", "buy")]

    results = {
        "sells": [],
        "buys": [],
        "errors": [],
        "executed_at": datetime.now().isoformat(),
        "dry_run": dry_run,
    }

    # Execute sells first
    print(f"\n{'='*60}")
    print(f"EXECUTING SELLS ({len(sells)} orders)")
    print(f"{'='*60}")

    for i, sell in enumerate(sells, 1):
        symbol = sell["symbol"]
        holding = holdings_map.get(symbol, {})
        quantity = holding.get("quantity", 0)

        if quantity <= 0:
            print(f"  [{i}/{len(sells)}] SKIP {symbol} - no shares to sell")
            continue

        value = sell["current_value"]
        print(f"\n  [{i}/{len(sells)}] SELL {symbol}")
        print(f"      Quantity: {quantity:.6f} shares")
        print(f"      Value:    ${value:,.2f}")

        if confirm_each and not dry_run:
            response = input("      Execute? (y/n/q): ").strip().lower()
            if response == 'q':
                print("      Aborting remaining trades.")
                break
            if response != 'y':
                print("      Skipped.")
                continue

        result = execute_sell(symbol, quantity, dry_run=dry_run)
        result["symbol"] = symbol
        result["intended_value"] = value
        result["quantity"] = quantity

        if result.get("state") == "queued" or result.get("status") == "dry_run":
            print(f"      OK - Order {'simulated' if dry_run else 'queued'}")
            results["sells"].append(result)
            log_trade({"type": "sell", **result})
        else:
            print(f"      ERROR: {result}")
            results["errors"].append(result)

        if not dry_run:
            time.sleep(0.5)  # Rate limiting

    # Execute buys
    print(f"\n{'='*60}")
    print(f"EXECUTING BUYS ({len(buys)} orders)")
    print(f"{'='*60}")

    for i, buy in enumerate(buys, 1):
        symbol = buy["symbol"]
        amount = buy["suggested_trade_value"]

        if amount < 1:
            print(f"  [{i}/{len(buys)}] SKIP {symbol} - amount too small (${amount:.2f})")
            continue

        print(f"\n  [{i}/{len(buys)}] BUY {symbol}")
        print(f"      Amount: ${amount:,.2f}")

        if confirm_each and not dry_run:
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

        if result.get("state") == "queued" or result.get("status") == "dry_run":
            print(f"      OK - Order {'simulated' if dry_run else 'queued'}")
            results["buys"].append(result)
            log_trade({"type": "buy", **result})
        else:
            print(f"      ERROR: {result}")
            results["errors"].append(result)

        if not dry_run:
            time.sleep(0.5)  # Rate limiting

    # Summary
    print(f"\n{'='*60}")
    print("EXECUTION SUMMARY")
    print(f"{'='*60}")
    print(f"  Sells executed: {len(results['sells'])}")
    print(f"  Buys executed:  {len(results['buys'])}")
    print(f"  Errors:         {len(results['errors'])}")
    print(f"  Mode:           {'DRY RUN' if dry_run else 'LIVE'}")

    return results


def show_pending_orders():
    """Display any pending orders"""
    ensure_logged_in()
    orders = get_pending_orders()

    if not orders:
        print("No pending orders.")
        return

    print(f"\nPending Orders ({len(orders)}):")
    for order in orders:
        symbol = order.get("symbol", "?")
        side = order.get("side", "?")
        qty = order.get("quantity", "?")
        state = order.get("state", "?")
        print(f"  {side.upper()} {symbol} x{qty} [{state}]")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "--dry-run":
            execute_all_trades(dry_run=True, confirm_each=False)
        elif sys.argv[1] == "--live":
            print("WARNING: This will execute REAL trades!")
            confirm = input("Type 'EXECUTE' to confirm: ")
            if confirm == "EXECUTE":
                execute_all_trades(dry_run=False, confirm_each=True)
            else:
                print("Aborted.")
        elif sys.argv[1] == "--pending":
            show_pending_orders()
    else:
        print("Usage:")
        print("  --dry-run   Simulate trades without executing")
        print("  --live      Execute real trades (with confirmation)")
        print("  --pending   Show pending orders")
