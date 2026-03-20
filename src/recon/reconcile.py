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
    ETFs/Index funds are excluded and kept as-is, UNLESS they are Bravos targets.

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

    # Get Bravos target symbols - these override ETF exclusion
    bravos_symbols = {t["symbol"] for t in targets}

    # Separate ETFs from individual stocks
    # BUT: if an ETF is a Bravos target, include it in stocks (not ETFs to keep)
    etf_holdings = [h for h in holdings if h["symbol"] in ETF_SYMBOLS and h["symbol"] not in bravos_symbols]
    stock_holdings = [h for h in holdings if h["symbol"] not in ETF_SYMBOLS or h["symbol"] in bravos_symbols]

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


def reconcile_fixed_dollar(dollars_per_weight: float = 500.0) -> ReconciliationResult:
    """
    Reconciliation using fixed dollar amounts per weight unit.

    Instead of allocating as % of portfolio, each Bravos weight unit = $X.
    e.g., weight 5 at $500/weight = $2,500 target position.

    Args:
        dollars_per_weight: Dollar amount per 1 weight unit (default $500)
    """
    # Load data
    targets = load_target_allocations()
    holdings, portfolio_value = load_current_holdings()

    # Load full holdings data
    holdings_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"
    with open(holdings_path) as f:
        holdings_data = json.load(f)

    # Get Bravos target symbols
    bravos_symbols = {t["symbol"] for t in targets}

    # Only reconcile Bravos positions (not ETFs unless they're Bravos targets)
    holdings_map = {h["symbol"]: h for h in holdings}

    # Calculate total Bravos target value
    total_weight = sum(t.get("weight", 0) for t in targets)
    total_target_value = total_weight * dollars_per_weight

    print(f"\n{'='*60}")
    print("FIXED-DOLLAR BRAVOS RECONCILIATION")
    print(f"{'='*60}")
    print(f"  Dollars per weight:  ${dollars_per_weight:,.0f}")
    print(f"  Total weight:        {total_weight}")
    print(f"  Target Bravos value: ${total_target_value:,.0f}")
    print(f"{'='*60}\n")

    deltas: list[ReconciliationDelta] = []

    # Process all Bravos targets
    for target in targets:
        symbol = target["symbol"]
        weight = target.get("weight", 0)
        target_value = weight * dollars_per_weight

        holding = holdings_map.get(symbol, {})
        current_value = holding.get("market_value", 0)

        delta_value = target_value - current_value

        # Determine action
        if current_value == 0 and target_value > 0:
            action = "enter"
        elif abs(delta_value) < 50:  # Within $50 is close enough
            action = "hold"
        elif delta_value > 0:
            action = "buy"
        else:
            action = "sell"

        # For percentage fields, use target value as % of total target
        target_pct = target_value / total_target_value if total_target_value > 0 else 0
        current_pct = current_value / total_target_value if total_target_value > 0 else 0

        notes = f"Weight {weight} → ${target_value:,.0f} target"
        if action == "sell":
            notes += f" (trim ${abs(delta_value):,.0f})"
        elif action == "buy":
            notes += f" (add ${delta_value:,.0f})"
        elif action == "enter":
            notes += f" (new position)"

        deltas.append({
            "symbol": symbol,
            "current_pct": round(current_pct, 4),
            "target_pct": round(target_pct, 4),
            "delta_pct": round((target_value - current_value) / total_target_value, 4) if total_target_value > 0 else 0,
            "action": action,
            "current_value": round(current_value, 2),
            "suggested_trade_value": round(delta_value, 2),
            "notes": notes,
        })

    # Check for positions to EXIT (in holdings but not in Bravos targets)
    for holding in holdings:
        symbol = holding["symbol"]
        if symbol in bravos_symbols:
            continue  # Already handled above
        if symbol in ETF_SYMBOLS:
            continue  # Keep ETFs
        if holding.get("market_value", 0) < 1:
            continue  # Skip zero positions

        # This is a stock position not in Bravos - EXIT
        current_value = holding.get("market_value", 0)
        deltas.append({
            "symbol": symbol,
            "current_pct": 0,
            "target_pct": 0,
            "delta_pct": 0,
            "action": "exit",
            "current_value": round(current_value, 2),
            "suggested_trade_value": round(-current_value, 2),
            "notes": f"Not in Bravos targets - EXIT ${current_value:,.0f}",
        })

    # Sort: exits and sells first (to free cash), then buys
    def sort_key(d):
        if d["action"] == "exit":
            return (0, -abs(d["suggested_trade_value"]))
        elif d["action"] == "sell":
            return (1, -abs(d["suggested_trade_value"]))
        elif d["action"] == "buy":
            return (2, -d["suggested_trade_value"])
        elif d["action"] == "enter":
            return (3, -d["suggested_trade_value"])
        else:
            return (4, 0)

    deltas.sort(key=sort_key)

    # Calculate summary
    total_sell = sum(abs(d["suggested_trade_value"]) for d in deltas if d["action"] in ("sell", "exit"))
    total_buy = sum(d["suggested_trade_value"] for d in deltas if d["action"] in ("buy", "enter"))

    summary = {
        "dollars_per_weight": dollars_per_weight,
        "total_weight": total_weight,
        "total_target_value": round(total_target_value, 2),
        "total_sell_value": round(total_sell, 2),
        "total_buy_value": round(total_buy, 2),
        "net_cash_flow": round(total_sell - total_buy, 2),
        "portfolio_value": round(portfolio_value, 2),
    }

    return {
        "deltas": deltas,
        "summary": summary,
        "generated_at": datetime.now().isoformat(),
    }


def run_fixed_dollar_reconciliation(dollars_per_weight: float = 500.0) -> ReconciliationResult:
    """Run fixed-dollar reconciliation and save results"""
    result = reconcile_fixed_dollar(dollars_per_weight)

    if result["deltas"]:
        save_reconciliation(result)

        summary = result["summary"]
        print(f"\n{'='*60}")
        print("RECONCILIATION SUMMARY")
        print(f"{'='*60}")
        print(f"  Target Bravos value: ${summary['total_target_value']:>10,.0f}")
        print(f"  ────────────────────────────────────────")
        print(f"  Total to SELL:       ${summary['total_sell_value']:>10,.0f}")
        print(f"  Total to BUY:        ${summary['total_buy_value']:>10,.0f}")
        print(f"  Net cash flow:       ${summary['net_cash_flow']:>+10,.0f}")
        print(f"{'='*60}")

        # Print sells first
        sells = [d for d in result["deltas"] if d["action"] in ("exit", "sell")]
        if sells:
            print("\n📤 SELL / EXIT:")
            for d in sells:
                print(f"  {d['action'].upper().ljust(5)} {d['symbol'].ljust(6)} ${abs(d['suggested_trade_value']):>8,.0f}  ({d['notes']})")

        # Then buys
        buys = [d for d in result["deltas"] if d["action"] in ("enter", "buy")]
        if buys:
            print("\n📥 BUY / ENTER:")
            for d in buys:
                print(f"  {d['action'].upper().ljust(5)} {d['symbol'].ljust(6)} ${d['suggested_trade_value']:>8,.0f}  ({d['notes']})")

        # Holds
        holds = [d for d in result["deltas"] if d["action"] == "hold"]
        if holds:
            print("\n✓ HOLD (within tolerance):")
            for d in holds:
                print(f"  {d['symbol'].ljust(6)} ${d['current_value']:>8,.0f}  ({d['notes']})")

    return result


def load_idea_prices() -> dict[str, dict]:
    """Load entry/target prices from bravos_trades.json (preferred) or idea_prices.json (fallback)"""

    # Try bravos_trades.json first (comprehensive trade data)
    trades_path = PROCESSED_DATA_DIR / "bravos_trades.json"
    if trades_path.exists():
        with open(trades_path) as f:
            data = json.load(f)

        price_map = {}
        for symbol, trade in data.get("trades", {}).items():
            price_map[symbol] = {
                "symbol": symbol,
                "entryPrice": trade.get("entryPrice"),
                "targetPrice": trade.get("targetPrice"),
                "stopLoss": trade.get("currentStop"),
                "entryDate": trade.get("entryDate"),
                "currentWeight": trade.get("currentWeight"),
            }

        if price_map:
            print(f"Loaded trade data from {trades_path}")
            return price_map

    # Fallback to idea_prices.json
    prices_path = PROCESSED_DATA_DIR / "idea_prices.json"

    if not prices_path.exists():
        print(f"No trade price data found")
        print("Run: npx tsx scripts/scrape-bravos-trades.ts first")
        return {}

    with open(prices_path) as f:
        data = json.load(f)

    # Build a map of symbol -> price data
    # Use the most recent entry for each symbol
    price_map = {}
    for entry in data:
        symbol = entry.get("symbol")
        if not symbol:
            continue

        # If we already have this symbol, keep the more recent one
        # or the one with an entry price
        if symbol in price_map:
            existing = price_map[symbol]
            # Prefer the one with entry price, or the more recent one
            if entry.get("entryPrice") and not existing.get("entryPrice"):
                price_map[symbol] = entry
            elif entry.get("entryDate") and existing.get("entryDate"):
                # Parse dates to compare (MM/DD/YYYY format)
                try:
                    from datetime import datetime as dt
                    entry_date = dt.strptime(entry["entryDate"], "%m/%d/%Y")
                    existing_date = dt.strptime(existing["entryDate"], "%m/%d/%Y")
                    if entry_date > existing_date:
                        price_map[symbol] = entry
                except ValueError:
                    pass
        else:
            price_map[symbol] = entry

    print(f"Loaded trade data from {prices_path} (fallback)")
    return price_map


def calculate_price_adjustment(
    entry_price: float | None,
    current_price: float,
    stop_loss: float | None = None,
    target_price: float | None = None,
    risk_reward_ratio: float = 2.0,
    default_target_pct: float = 0.30,
    min_allocation: float = 0.10,
    max_allocation: float = 1.20,
) -> tuple[float, str]:
    """
    Calculate allocation adjustment based on remaining upside to target.

    Uses stop loss to infer target if not provided:
    - Risk = entry - stop
    - Reward = risk * risk_reward_ratio
    - Target = entry + reward

    Returns:
        (adjustment_factor, explanation_string)
        - adjustment_factor: 1.0 = full allocation, 0.5 = half, etc.
    """
    if entry_price is None or entry_price <= 0:
        return 1.0, "No entry price"

    if current_price <= 0:
        return 1.0, "No current price"

    # Calculate or infer target price
    if target_price and target_price > entry_price:
        inferred_target = target_price
        target_source = "explicit"
    elif stop_loss and stop_loss < entry_price:
        # Stop must be meaningfully below entry (at least 2% risk)
        risk = entry_price - stop_loss
        risk_pct = risk / entry_price

        if risk_pct >= 0.02:  # At least 2% downside risk
            reward = risk * risk_reward_ratio
            inferred_target = entry_price + reward
            target_source = f"{risk_reward_ratio:.1f}:1 R/R"
        else:
            # Stop too tight, use default
            inferred_target = entry_price * (1 + default_target_pct)
            target_source = f"default {default_target_pct:.0%}"
    else:
        # No valid stop (or stop >= entry, which means trailing stop)
        # Fallback: assume default upside target from entry
        inferred_target = entry_price * (1 + default_target_pct)
        target_source = f"default {default_target_pct:.0%}"

    # Calculate original and remaining upside
    original_upside_pct = (inferred_target - entry_price) / entry_price

    if current_price >= inferred_target:
        # Already at or above target - minimal allocation
        return min_allocation, f"At/above target ${inferred_target:.2f} ({target_source}) → {min_allocation:.0%}"

    remaining_upside_pct = (inferred_target - current_price) / current_price

    # Thesis remaining = remaining upside / original upside
    # But we need to account for the fact that remaining is calculated from current, not entry
    # So we compare absolute dollar amounts
    original_upside_dollars = inferred_target - entry_price
    remaining_upside_dollars = inferred_target - current_price

    if original_upside_dollars <= 0:
        return 1.0, "Invalid target"

    thesis_remaining = remaining_upside_dollars / original_upside_dollars
    thesis_remaining = max(0, min(1.5, thesis_remaining))  # Cap between 0 and 150%

    # If stock is DOWN from entry, thesis_remaining > 1.0 (more upside than originally)
    # Cap the allocation adjustment
    adjustment = max(min_allocation, min(max_allocation, thesis_remaining))

    # Build explanation
    price_change_pct = (current_price - entry_price) / entry_price
    if price_change_pct >= 0:
        direction = f"Up {price_change_pct:.1%}"
    else:
        direction = f"Down {abs(price_change_pct):.1%}"

    explanation = (
        f"{direction} from ${entry_price:.2f}, "
        f"target ${inferred_target:.2f} ({target_source}), "
        f"{thesis_remaining:.0%} remaining → {adjustment:.0%}"
    )

    return adjustment, explanation


def reconcile_price_adjusted(
    dollars_per_weight: float = 500.0,
    apply_adjustment: bool = True,
    default_target_pct: float = 0.30,
    risk_reward_ratio: float = 2.0,
) -> ReconciliationResult:
    """
    Reconciliation with price-adjusted allocation based on entry prices.

    If a stock has run up significantly from Bravos entry, we allocate less.
    If a stock is down from entry, we allocate full amount (or slightly more).

    Args:
        dollars_per_weight: Base dollar amount per weight unit
        apply_adjustment: If False, just shows what adjustments would be made
    """
    # Load data
    targets = load_target_allocations()
    holdings, portfolio_value = load_current_holdings()
    idea_prices = load_idea_prices()

    # Load full holdings data
    holdings_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"
    with open(holdings_path) as f:
        holdings_data = json.load(f)

    # Get Bravos target symbols
    bravos_symbols = {t["symbol"] for t in targets}

    # Build holdings map with current prices
    holdings_map = {}
    for h in holdings:
        symbol = h["symbol"]
        shares = h.get("quantity", 0)
        market_value = h.get("market_value", 0)
        # Calculate current price from holdings
        current_price = market_value / shares if shares > 0 else 0
        holdings_map[symbol] = {
            **h,
            "current_price": current_price,
        }

    # Calculate totals
    total_weight = sum(t.get("weight", 0) for t in targets)
    base_target_value = total_weight * dollars_per_weight

    print(f"\n{'='*60}")
    print("PRICE-ADJUSTED BRAVOS RECONCILIATION")
    print(f"{'='*60}")
    print(f"  Base dollars per weight: ${dollars_per_weight:,.0f}")
    print(f"  Total weight:            {total_weight}")
    print(f"  Base target value:       ${base_target_value:,.0f}")
    print(f"  Default target upside:   {default_target_pct:.0%}")
    print(f"  Risk/Reward ratio:       {risk_reward_ratio:.1f}:1")
    print(f"  Price adjustment:        {'ENABLED' if apply_adjustment else 'DISABLED'}")
    print(f"{'='*60}\n")

    deltas: list[ReconciliationDelta] = []
    adjustments_summary = []

    for target in targets:
        symbol = target["symbol"]
        weight = target.get("weight", 0)
        base_target = weight * dollars_per_weight

        holding = holdings_map.get(symbol, {})
        current_value = holding.get("market_value", 0)
        current_price = holding.get("current_price", 0)

        # Get entry price, stop loss, and target
        price_data = idea_prices.get(symbol, {})
        entry_price = price_data.get("entryPrice")
        stop_loss = price_data.get("stopLoss")
        target_price = price_data.get("targetPrice")
        entry_date = price_data.get("entryDate", "Unknown")

        # Calculate adjustment using stop-loss-inferred targets
        if entry_price and current_price > 0:
            adjustment, explanation = calculate_price_adjustment(
                entry_price=entry_price,
                current_price=current_price,
                stop_loss=stop_loss,
                target_price=target_price,
                risk_reward_ratio=risk_reward_ratio,
                default_target_pct=default_target_pct,
            )
        else:
            adjustment = 1.0
            explanation = "No price data"

        # Apply adjustment to target
        adjusted_target = base_target * adjustment if apply_adjustment else base_target
        delta_value = adjusted_target - current_value

        # Determine action
        if current_value == 0 and adjusted_target > 0:
            action = "enter"
        elif abs(delta_value) < 50:
            action = "hold"
        elif delta_value > 0:
            action = "buy"
        else:
            action = "sell"

        # Track adjustment info
        adjustments_summary.append({
            "symbol": symbol,
            "weight": weight,
            "entry_price": entry_price,
            "entry_date": entry_date,
            "current_price": current_price,
            "adjustment": adjustment,
            "base_target": base_target,
            "adjusted_target": adjusted_target,
            "explanation": explanation,
        })

        # Build notes
        notes = f"Weight {weight} → ${adjusted_target:,.0f}"
        if adjustment != 1.0:
            notes += f" ({adjustment:.0%} of ${base_target:,.0f})"
        if action == "sell":
            notes += f" (trim ${abs(delta_value):,.0f})"
        elif action == "buy":
            notes += f" (add ${delta_value:,.0f})"
        elif action == "enter":
            notes += f" (new position)"

        deltas.append({
            "symbol": symbol,
            "current_pct": 0,  # Not using % for fixed-dollar
            "target_pct": 0,
            "delta_pct": 0,
            "action": action,
            "current_value": round(current_value, 2),
            "suggested_trade_value": round(delta_value, 2),
            "notes": notes,
        })

    # Check for positions to EXIT
    for holding in holdings:
        symbol = holding["symbol"]
        if symbol in bravos_symbols:
            continue
        if symbol in ETF_SYMBOLS:
            continue
        if holding.get("market_value", 0) < 1:
            continue

        current_value = holding.get("market_value", 0)
        deltas.append({
            "symbol": symbol,
            "current_pct": 0,
            "target_pct": 0,
            "delta_pct": 0,
            "action": "exit",
            "current_value": round(current_value, 2),
            "suggested_trade_value": round(-current_value, 2),
            "notes": f"Not in Bravos targets - EXIT ${current_value:,.0f}",
        })

    # Sort: exits and sells first, then buys
    def sort_key(d):
        if d["action"] == "exit":
            return (0, -abs(d["suggested_trade_value"]))
        elif d["action"] == "sell":
            return (1, -abs(d["suggested_trade_value"]))
        elif d["action"] == "buy":
            return (2, -d["suggested_trade_value"])
        elif d["action"] == "enter":
            return (3, -d["suggested_trade_value"])
        else:
            return (4, 0)

    deltas.sort(key=sort_key)

    # Print adjustment summary
    print("Price Adjustments:")
    print("-" * 80)
    print(f"{'Symbol':<8} {'Weight':>6} {'Entry':>10} {'Current':>10} {'Adj':>6} {'Base':>10} {'Adjusted':>10}")
    print("-" * 80)
    for adj in sorted(adjustments_summary, key=lambda x: x["adjustment"]):
        entry_str = f"${adj['entry_price']:.2f}" if adj['entry_price'] else "N/A"
        current_str = f"${adj['current_price']:.2f}" if adj['current_price'] > 0 else "N/A"
        print(f"{adj['symbol']:<8} {adj['weight']:>6} {entry_str:>10} {current_str:>10} "
              f"{adj['adjustment']:>5.0%} ${adj['base_target']:>9,.0f} ${adj['adjusted_target']:>9,.0f}")
    print("-" * 80)

    # Calculate summary
    total_sell = sum(abs(d["suggested_trade_value"]) for d in deltas if d["action"] in ("sell", "exit"))
    total_buy = sum(d["suggested_trade_value"] for d in deltas if d["action"] in ("buy", "enter"))
    total_adjusted_target = sum(a["adjusted_target"] for a in adjustments_summary)

    summary = {
        "dollars_per_weight": dollars_per_weight,
        "total_weight": total_weight,
        "base_target_value": round(base_target_value, 2),
        "adjusted_target_value": round(total_adjusted_target, 2),
        "total_sell_value": round(total_sell, 2),
        "total_buy_value": round(total_buy, 2),
        "net_cash_flow": round(total_sell - total_buy, 2),
        "portfolio_value": round(portfolio_value, 2),
        "price_adjustment_enabled": apply_adjustment,
    }

    return {
        "deltas": deltas,
        "summary": summary,
        "generated_at": datetime.now().isoformat(),
    }


def run_price_adjusted_reconciliation(
    dollars_per_weight: float = 500.0,
    default_target_pct: float = 0.30,
) -> ReconciliationResult:
    """Run price-adjusted reconciliation and save results"""
    result = reconcile_price_adjusted(
        dollars_per_weight,
        apply_adjustment=True,
        default_target_pct=default_target_pct,
    )

    if result["deltas"]:
        save_reconciliation(result)

        summary = result["summary"]
        print(f"\n{'='*60}")
        print("RECONCILIATION SUMMARY (Price-Adjusted)")
        print(f"{'='*60}")
        print(f"  Base target value:     ${summary['base_target_value']:>10,.0f}")
        print(f"  Adjusted target value: ${summary['adjusted_target_value']:>10,.0f}")
        print(f"  ────────────────────────────────────────")
        print(f"  Total to SELL:         ${summary['total_sell_value']:>10,.0f}")
        print(f"  Total to BUY:          ${summary['total_buy_value']:>10,.0f}")
        print(f"  Net cash flow:         ${summary['net_cash_flow']:>+10,.0f}")
        print(f"{'='*60}")

        # Print sells first
        sells = [d for d in result["deltas"] if d["action"] in ("exit", "sell")]
        if sells:
            print("\n📤 SELL / EXIT:")
            for d in sells:
                print(f"  {d['action'].upper().ljust(5)} {d['symbol'].ljust(6)} ${abs(d['suggested_trade_value']):>8,.0f}  ({d['notes']})")

        # Then buys
        buys = [d for d in result["deltas"] if d["action"] in ("enter", "buy")]
        if buys:
            print("\n📥 BUY / ENTER:")
            for d in buys:
                print(f"  {d['action'].upper().ljust(5)} {d['symbol'].ljust(6)} ${d['suggested_trade_value']:>8,.0f}  ({d['notes']})")

    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--stocks-only":
        run_stocks_only_reconciliation()
    elif len(sys.argv) > 1 and sys.argv[1] == "--fixed-dollar":
        # Optional: pass dollars per weight as second arg
        dpw = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
        run_fixed_dollar_reconciliation(dpw)
    elif len(sys.argv) > 1 and sys.argv[1] == "--price-adjusted":
        # Usage: --price-adjusted [dollars_per_weight] [target_pct]
        # e.g., --price-adjusted 500 0.30
        dpw = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
        target_pct = float(sys.argv[3]) if len(sys.argv) > 3 else 0.30
        run_price_adjusted_reconciliation(dpw, target_pct)
    else:
        run_reconciliation()
