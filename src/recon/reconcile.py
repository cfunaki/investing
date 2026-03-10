"""
Reconcile Bravos target allocations with Robinhood holdings.
Compares allocation percentages and generates delta actions.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import TypedDict, Literal


class ReconciliationDelta(TypedDict):
    symbol: str
    current_pct: float
    target_pct: float
    delta_pct: float
    action: Literal["buy", "sell", "hold", "exit", "enter"]
    current_value: float
    suggested_trade_value: float
    notes: str


class ReconciliationResult(TypedDict):
    deltas: list[ReconciliationDelta]
    summary: dict
    generated_at: str


PROCESSED_DATA_DIR = Path("data/processed")

# Threshold for "hold" action (within this % is considered matched)
HOLD_THRESHOLD = 0.005  # 0.5%

# ETFs and Index Funds to EXCLUDE from Bravos reconciliation
# These will be kept as-is and not aligned with Bravos targets
ETF_SYMBOLS = {
    # Broad Market ETFs
    "SPY", "QQQ", "DIA", "IWM", "VTI", "VOO",
    # International ETFs
    "VXUS", "VGK", "VWO", "ASEA", "GREK", "EWJ", "EEM", "MCHI", "INDA",
    # Sector ETFs
    "VGT", "XME", "XLF", "XLE", "XLK", "XLU", "XBI",
    # Commodity ETFs
    "SLV", "GLD", "CPER", "DBC", "USO", "UNG",
    # Currency ETFs
    "YCS", "UUP", "FXE",
    # Bond ETFs
    "TLT", "BND", "AGG", "HYG",
    # Leveraged/Inverse ETFs
    "TQQQ", "SQQQ", "SPXS", "UPRO", "SVIX",
}


def load_target_allocations() -> list[dict]:
    """Load target allocations from Bravos data"""
    alloc_path = PROCESSED_DATA_DIR / "target_allocations.json"

    if not alloc_path.exists():
        print(f"No target allocations found at {alloc_path}")
        print("Run derive_positions.py first")
        return []

    with open(alloc_path) as f:
        data = json.load(f)

    return data.get("allocations", [])


def load_current_holdings() -> tuple[list[dict], float]:
    """Load current holdings and total portfolio value"""
    holdings_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"

    if not holdings_path.exists():
        print(f"No holdings data found at {holdings_path}")
        print("Run holdings.py first to fetch from Robinhood")
        return [], 0

    with open(holdings_path) as f:
        data = json.load(f)

    holdings = data.get("holdings", [])
    total_value = data.get("total_value", 0)

    # If we have account info, use portfolio value instead
    account = data.get("account")
    if account and account.get("portfolio_value"):
        total_value = account["portfolio_value"]

    return holdings, total_value


def determine_action(current_pct: float, target_pct: float) -> str:
    """Determine the reconciliation action based on delta"""
    delta = target_pct - current_pct

    if current_pct == 0 and target_pct > 0:
        return "enter"  # New position to add
    elif target_pct == 0 and current_pct > 0:
        return "exit"  # Position to fully close
    elif abs(delta) <= HOLD_THRESHOLD:
        return "hold"  # Within threshold, no action needed
    elif delta > 0:
        return "buy"  # Need to increase position
    else:
        return "sell"  # Need to decrease position


def reconcile() -> ReconciliationResult:
    """
    Main reconciliation logic.
    Compares target allocations to current holdings and generates deltas.
    """
    # Load data
    targets = load_target_allocations()
    holdings, portfolio_value = load_current_holdings()

    print(f"Loaded {len(targets)} target allocations")
    print(f"Loaded {len(holdings)} current holdings")
    print(f"Portfolio value: ${portfolio_value:,.2f}")

    if not targets:
        print("No targets to reconcile")
        return {
            "deltas": [],
            "summary": {},
            "generated_at": datetime.now().isoformat(),
        }

    # Build lookup maps
    target_map = {t["symbol"]: t for t in targets}
    holdings_map = {h["symbol"]: h for h in holdings}

    # Get all symbols (union of targets and holdings)
    all_symbols = set(target_map.keys()) | set(holdings_map.keys())

    deltas: list[ReconciliationDelta] = []

    for symbol in sorted(all_symbols):
        target = target_map.get(symbol, {})
        holding = holdings_map.get(symbol, {})

        target_pct = target.get("target_pct", 0)
        current_pct = holding.get("current_pct", 0)
        current_value = holding.get("market_value", 0)

        delta_pct = target_pct - current_pct
        action = determine_action(current_pct, target_pct)

        # Calculate suggested trade value
        target_value = portfolio_value * target_pct
        suggested_trade_value = target_value - current_value

        # Build notes
        notes = []
        if action == "enter":
            notes.append(f"New position: allocate {target_pct:.1%}")
        elif action == "exit":
            notes.append(f"Not in target: close ${current_value:,.0f} position")
        elif action == "buy":
            notes.append(f"Underweight by {abs(delta_pct):.1%}")
        elif action == "sell":
            notes.append(f"Overweight by {abs(delta_pct):.1%}")

        # Check for short positions
        if target.get("side") == "short":
            notes.append("SHORT position in target")

        deltas.append({
            "symbol": symbol,
            "current_pct": round(current_pct, 4),
            "target_pct": round(target_pct, 4),
            "delta_pct": round(delta_pct, 4),
            "action": action,
            "current_value": round(current_value, 2),
            "suggested_trade_value": round(suggested_trade_value, 2),
            "notes": "; ".join(notes) if notes else "",
        })

    # Sort by absolute delta (largest mismatches first)
    deltas.sort(key=lambda d: abs(d["delta_pct"]), reverse=True)

    # Generate summary
    actions = [d["action"] for d in deltas]
    total_buy = sum(d["suggested_trade_value"] for d in deltas if d["action"] in ("buy", "enter"))
    total_sell = sum(abs(d["suggested_trade_value"]) for d in deltas if d["action"] in ("sell", "exit"))

    summary = {
        "total_symbols": len(deltas),
        "buys_needed": actions.count("buy") + actions.count("enter"),
        "sells_needed": actions.count("sell") + actions.count("exit"),
        "holds": actions.count("hold"),
        "total_buy_value": round(total_buy, 2),
        "total_sell_value": round(total_sell, 2),
        "net_cash_flow": round(total_sell - total_buy, 2),
        "portfolio_value": round(portfolio_value, 2),
    }

    result: ReconciliationResult = {
        "deltas": deltas,
        "summary": summary,
        "generated_at": datetime.now().isoformat(),
    }

    return result


def reconcile_stocks_only(include_cash: bool = True) -> ReconciliationResult:
    """
    Reconciliation that ONLY aligns individual stocks with Bravos.
    ETFs/Index funds are excluded and kept as-is.

    Args:
        include_cash: If True, available cash is included in the pool for Bravos stocks
    """
    # Load data
    targets = load_target_allocations()
    holdings, portfolio_value = load_current_holdings()

    # Load full holdings data to get cash
    holdings_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"
    with open(holdings_path) as f:
        holdings_data = json.load(f)

    account = holdings_data.get("account", {})
    cash = account.get("cash", 0)

    # Separate ETFs from individual stocks
    etf_holdings = [h for h in holdings if h["symbol"] in ETF_SYMBOLS]
    stock_holdings = [h for h in holdings if h["symbol"] not in ETF_SYMBOLS]

    etf_value = sum(h["market_value"] for h in etf_holdings)
    stock_value = sum(h["market_value"] for h in stock_holdings)

    # Calculate the pool available for Bravos stocks
    if include_cash:
        bravos_pool = stock_value + cash
    else:
        bravos_pool = stock_value

    total_portfolio = etf_value + stock_value + cash

    print(f"\n{'='*60}")
    print("PORTFOLIO BREAKDOWN")
    print(f"{'='*60}")
    print(f"  Cash:              ${cash:>12,.2f}  ({cash/total_portfolio:>6.1%})")
    print(f"  ETFs (keep):       ${etf_value:>12,.2f}  ({etf_value/total_portfolio:>6.1%})")
    print(f"  Stocks (rebalance):${stock_value:>12,.2f}  ({stock_value/total_portfolio:>6.1%})")
    print(f"  {'─'*40}")
    print(f"  Total Portfolio:   ${total_portfolio:>12,.2f}")
    print(f"\n  Bravos Pool:       ${bravos_pool:>12,.2f}  (stocks + cash)")
    print(f"{'='*60}\n")

    print(f"ETFs to KEEP ({len(etf_holdings)}):")
    for h in sorted(etf_holdings, key=lambda x: -x["market_value"]):
        print(f"  {h['symbol'].ljust(6)} ${h['market_value']:>10,.2f}")

    print(f"\nIndividual stocks to RECONCILE ({len(stock_holdings)}):")
    for h in sorted(stock_holdings, key=lambda x: -x["market_value"]):
        print(f"  {h['symbol'].ljust(6)} ${h['market_value']:>10,.2f}")

    if not targets:
        print("No targets to reconcile")
        return {"deltas": [], "summary": {}, "generated_at": datetime.now().isoformat()}

    # Build lookup maps (only for individual stocks, not ETFs)
    target_map = {t["symbol"]: t for t in targets}
    stock_holdings_map = {h["symbol"]: h for h in stock_holdings}

    # Get all symbols to reconcile (targets + current stocks, excluding ETFs)
    all_symbols = set(target_map.keys()) | set(stock_holdings_map.keys())

    deltas: list[ReconciliationDelta] = []

    for symbol in sorted(all_symbols):
        target = target_map.get(symbol, {})
        holding = stock_holdings_map.get(symbol, {})

        # Target % is relative to Bravos pool
        target_pct_of_bravos = target.get("target_pct", 0)

        # Current % is also relative to Bravos pool
        current_value = holding.get("market_value", 0)
        current_pct_of_bravos = current_value / bravos_pool if bravos_pool > 0 else 0

        delta_pct = target_pct_of_bravos - current_pct_of_bravos
        action = determine_action(current_pct_of_bravos, target_pct_of_bravos)

        # Calculate suggested trade value based on Bravos pool
        target_value = bravos_pool * target_pct_of_bravos
        suggested_trade_value = target_value - current_value

        # Build notes
        notes = []
        if action == "enter":
            notes.append(f"New position: allocate {target_pct_of_bravos:.1%} of stocks pool (${target_value:,.0f})")
        elif action == "exit":
            notes.append(f"Not in Bravos: sell ${current_value:,.0f}")
        elif action == "buy":
            notes.append(f"Underweight by {abs(delta_pct):.1%}")
        elif action == "sell":
            notes.append(f"Overweight by {abs(delta_pct):.1%}")

        if target.get("side") == "short":
            notes.append("SHORT position")

        deltas.append({
            "symbol": symbol,
            "current_pct": round(current_pct_of_bravos, 4),
            "target_pct": round(target_pct_of_bravos, 4),
            "delta_pct": round(delta_pct, 4),
            "action": action,
            "current_value": round(current_value, 2),
            "suggested_trade_value": round(suggested_trade_value, 2),
            "notes": "; ".join(notes) if notes else "",
        })

    # Sort: exits first, then enters, by value
    deltas.sort(key=lambda d: (
        0 if d["action"] == "exit" else 1,
        -abs(d["suggested_trade_value"])
    ))

    # Generate summary
    actions = [d["action"] for d in deltas]
    total_buy = sum(d["suggested_trade_value"] for d in deltas if d["action"] in ("buy", "enter"))
    total_sell = sum(abs(d["suggested_trade_value"]) for d in deltas if d["action"] in ("sell", "exit"))

    summary = {
        "total_symbols": len(deltas),
        "buys_needed": actions.count("buy") + actions.count("enter"),
        "sells_needed": actions.count("sell") + actions.count("exit"),
        "holds": actions.count("hold"),
        "total_buy_value": round(total_buy, 2),
        "total_sell_value": round(total_sell, 2),
        "net_cash_flow": round(total_sell - total_buy, 2),
        "bravos_pool": round(bravos_pool, 2),
        "cash_available": round(cash, 2),
        "etf_value_kept": round(etf_value, 2),
        "portfolio_value": round(total_portfolio, 2),
    }

    return {
        "deltas": deltas,
        "summary": summary,
        "generated_at": datetime.now().isoformat(),
    }


def save_reconciliation(result: ReconciliationResult) -> str:
    """Save reconciliation results to disk"""
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)

    output_path = PROCESSED_DATA_DIR / "reconciliation.json"

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Saved reconciliation to {output_path}")
    return str(output_path)


def run_reconciliation() -> ReconciliationResult:
    """Main entry point: run reconciliation and save results"""
    result = reconcile()

    if result["deltas"]:
        save_reconciliation(result)

        # Print summary
        summary = result["summary"]
        print("\nReconciliation Summary:")
        print("-" * 50)
        print(f"  Total symbols:    {summary['total_symbols']}")
        print(f"  Buys needed:      {summary['buys_needed']}")
        print(f"  Sells needed:     {summary['sells_needed']}")
        print(f"  Holds (in-line):  {summary['holds']}")
        print(f"  Est. buy value:   ${summary['total_buy_value']:,.2f}")
        print(f"  Est. sell value:  ${summary['total_sell_value']:,.2f}")
        print(f"  Net cash flow:    ${summary['net_cash_flow']:+,.2f}")
        print("-" * 50)

        # Print actionable items
        print("\nActions Required:")
        for delta in result["deltas"]:
            if delta["action"] != "hold":
                pct_change = f"{delta['delta_pct']:+.1%}"
                value_change = f"${delta['suggested_trade_value']:+,.0f}"
                print(f"  {delta['action'].upper().ljust(5)} {delta['symbol'].ljust(6)} {pct_change.rjust(7)} ({value_change})")

    return result


def run_stocks_only_reconciliation() -> ReconciliationResult:
    """Run reconciliation for individual stocks only (excludes ETFs)"""
    result = reconcile_stocks_only(include_cash=True)

    if result["deltas"]:
        save_reconciliation(result)

        summary = result["summary"]
        print(f"\n{'='*60}")
        print("RECONCILIATION SUMMARY (Stocks Only)")
        print(f"{'='*60}")
        print(f"  Bravos pool:      ${summary['bravos_pool']:>12,.2f}")
        print(f"  Cash available:   ${summary['cash_available']:>12,.2f}")
        print(f"  ETFs kept:        ${summary['etf_value_kept']:>12,.2f}")
        print(f"  {'─'*40}")
        print(f"  Stocks to sell:   {summary['sells_needed']:>3} (${summary['total_sell_value']:>10,.2f})")
        print(f"  Stocks to buy:    {summary['buys_needed']:>3} (${summary['total_buy_value']:>10,.2f})")
        print(f"  Net cash needed:  ${-summary['net_cash_flow']:>10,.2f}")
        print(f"{'='*60}")

        # Print sells first
        sells = [d for d in result["deltas"] if d["action"] in ("exit", "sell")]
        if sells:
            print("\n📤 SELL / EXIT:")
            for d in sells:
                print(f"  {d['action'].upper().ljust(5)} {d['symbol'].ljust(6)} ${abs(d['suggested_trade_value']):>10,.2f}  ({d['notes']})")

        # Then buys
        buys = [d for d in result["deltas"] if d["action"] in ("enter", "buy")]
        if buys:
            print("\n📥 BUY / ENTER:")
            for d in buys:
                print(f"  {d['action'].upper().ljust(5)} {d['symbol'].ljust(6)} ${d['suggested_trade_value']:>10,.2f}  ({d['notes']})")

    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--stocks-only":
        run_stocks_only_reconciliation()
    else:
        run_reconciliation()
