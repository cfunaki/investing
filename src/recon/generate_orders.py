"""
Generate proposed order objects from reconciliation deltas.
MVP: Output only - no execution.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import TypedDict, Literal, Optional


class ProposedOrder(TypedDict):
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    notional: Optional[float]  # Dollar amount for fractional shares
    quantity: Optional[int]  # Share count for whole shares
    limit_price: Optional[float]
    rationale: str
    priority: int  # 1 = highest priority
    warnings: list[str]


PROCESSED_DATA_DIR = Path("data/processed")

# Minimum trade value to generate an order
MIN_TRADE_VALUE = 10.0


def load_reconciliation() -> dict:
    """Load reconciliation results"""
    recon_path = PROCESSED_DATA_DIR / "reconciliation.json"

    if not recon_path.exists():
        print(f"No reconciliation data found at {recon_path}")
        print("Run reconcile.py first")
        return {}

    with open(recon_path) as f:
        return json.load(f)


def generate_orders() -> list[ProposedOrder]:
    """
    Generate proposed orders from reconciliation deltas.

    MVP: Generates order objects for review only.
    Does NOT execute any trades.
    """
    recon_data = load_reconciliation()

    if not recon_data:
        return []

    deltas = recon_data.get("deltas", [])
    orders: list[ProposedOrder] = []

    # Priority counter
    priority = 1

    for delta in deltas:
        action = delta["action"]
        symbol = delta["symbol"]
        trade_value = abs(delta["suggested_trade_value"])

        # Skip holds and small trades
        if action == "hold":
            continue

        if trade_value < MIN_TRADE_VALUE:
            continue

        warnings: list[str] = []

        # Determine side
        if action in ("buy", "enter"):
            side = "buy"
        else:
            side = "sell"

        # Build rationale
        if action == "enter":
            rationale = f"New position: {delta['target_pct']:.1%} target allocation"
        elif action == "exit":
            rationale = f"Exit position: not in target portfolio"
            warnings.append("Full position exit - verify this is intentional")
        elif action == "buy":
            rationale = f"Increase allocation from {delta['current_pct']:.1%} to {delta['target_pct']:.1%}"
        else:  # sell
            rationale = f"Reduce allocation from {delta['current_pct']:.1%} to {delta['target_pct']:.1%}"

        # Add warning for large trades
        if trade_value > 10000:
            warnings.append(f"Large trade: ${trade_value:,.0f}")

        # Check for short warnings
        if "SHORT" in delta.get("notes", ""):
            warnings.append("Target is SHORT position - special handling required")

        order: ProposedOrder = {
            "symbol": symbol,
            "side": side,
            "order_type": "market",  # MVP uses market orders
            "notional": round(trade_value, 2),
            "quantity": None,  # Use notional for fractional shares
            "limit_price": None,
            "rationale": rationale,
            "priority": priority,
            "warnings": warnings,
        }

        orders.append(order)
        priority += 1

    return orders


def save_orders(orders: list[ProposedOrder]) -> str:
    """Save proposed orders to disk"""
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)

    output_path = PROCESSED_DATA_DIR / "proposed_orders.json"

    data = {
        "orders": orders,
        "count": len(orders),
        "generated_at": datetime.now().isoformat(),
        "status": "PENDING_REVIEW",
        "warning": "These orders are for REVIEW ONLY. Execution is disabled in MVP.",
    }

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Saved proposed orders to {output_path}")
    return str(output_path)


def run_order_generation() -> list[ProposedOrder]:
    """Main entry point: generate orders from reconciliation data"""
    orders = generate_orders()

    if orders:
        save_orders(orders)

        # Print summary
        print("\nProposed Orders (REVIEW ONLY - NOT EXECUTED):")
        print("=" * 60)

        buy_orders = [o for o in orders if o["side"] == "buy"]
        sell_orders = [o for o in orders if o["side"] == "sell"]

        if sell_orders:
            print("\nSELL Orders:")
            print("-" * 40)
            for order in sell_orders:
                warning_str = " ⚠️" if order["warnings"] else ""
                print(f"  #{order['priority']} {order['symbol'].ljust(6)} ${order['notional']:,.2f}{warning_str}")
                print(f"      {order['rationale']}")
                for warn in order["warnings"]:
                    print(f"      ⚠️ {warn}")

        if buy_orders:
            print("\nBUY Orders:")
            print("-" * 40)
            for order in buy_orders:
                warning_str = " ⚠️" if order["warnings"] else ""
                print(f"  #{order['priority']} {order['symbol'].ljust(6)} ${order['notional']:,.2f}{warning_str}")
                print(f"      {order['rationale']}")
                for warn in order["warnings"]:
                    print(f"      ⚠️ {warn}")

        print("\n" + "=" * 60)
        print("⚠️  ORDERS ARE FOR REVIEW ONLY - EXECUTION DISABLED IN MVP")
        print("=" * 60)

    else:
        print("No orders to generate")

    return orders


if __name__ == "__main__":
    run_order_generation()
