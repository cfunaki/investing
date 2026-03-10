"""
Generate human-readable markdown reconciliation report.
"""

import json
from datetime import datetime
from pathlib import Path


PROCESSED_DATA_DIR = Path("data/processed")
REPORTS_DIR = Path("data/reports")


def load_data() -> dict:
    """Load all processed data files"""
    data = {}

    files = {
        "ideas": "ideas.json",
        "allocations": "target_allocations.json",
        "holdings": "robinhood_holdings.json",
        "reconciliation": "reconciliation.json",
        "orders": "proposed_orders.json",
    }

    for key, filename in files.items():
        filepath = PROCESSED_DATA_DIR / filename
        if filepath.exists():
            with open(filepath) as f:
                data[key] = json.load(f)

    return data


def generate_markdown_report() -> str:
    """Generate a comprehensive markdown report"""
    data = load_data()

    lines = []

    # Header
    lines.append("# Portfolio Reconciliation Report")
    lines.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Summary section
    if "reconciliation" in data:
        summary = data["reconciliation"].get("summary", {})
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- **Portfolio Value**: ${summary.get('portfolio_value', 0):,.2f}")
        lines.append(f"- **Total Symbols**: {summary.get('total_symbols', 0)}")
        lines.append(f"- **Buys Needed**: {summary.get('buys_needed', 0)}")
        lines.append(f"- **Sells Needed**: {summary.get('sells_needed', 0)}")
        lines.append(f"- **Positions Aligned**: {summary.get('holds', 0)}")
        lines.append(f"- **Est. Buy Value**: ${summary.get('total_buy_value', 0):,.2f}")
        lines.append(f"- **Est. Sell Value**: ${summary.get('total_sell_value', 0):,.2f}")
        lines.append(f"- **Net Cash Flow**: ${summary.get('net_cash_flow', 0):+,.2f}")
        lines.append("")

    # Current Holdings section
    if "holdings" in data:
        holdings = data["holdings"].get("holdings", [])
        if holdings:
            lines.append("## Current Robinhood Holdings")
            lines.append("")
            lines.append("| Symbol | Allocation | Value | P/L |")
            lines.append("|--------|------------|-------|-----|")

            for h in holdings:
                symbol = h["symbol"]
                pct = f"{h['current_pct']:.1%}"
                value = f"${h['market_value']:,.0f}"
                pl = f"{h['unrealized_pl_pct']:+.1f}%"
                lines.append(f"| {symbol} | {pct} | {value} | {pl} |")

            total = sum(h["market_value"] for h in holdings)
            lines.append(f"| **Total** | **100%** | **${total:,.0f}** | |")
            lines.append("")

    # Target Allocations section
    if "allocations" in data:
        allocations = data["allocations"].get("allocations", [])
        if allocations:
            lines.append("## Target Allocations (from Bravos)")
            lines.append("")
            lines.append("| Symbol | Target | Side | # Ideas |")
            lines.append("|--------|--------|------|---------|")

            for a in allocations:
                symbol = a["symbol"]
                pct = f"{a['target_pct']:.1%}"
                side = "SHORT" if a["side"] == "short" else "LONG"
                count = a["idea_count"]
                lines.append(f"| {symbol} | {pct} | {side} | {count} |")

            # Show validation info
            validation = data["allocations"].get("validation", {})
            total_alloc = validation.get("total_allocation_pct", 0)
            cash = validation.get("implied_cash_pct", 0)
            lines.append("")
            lines.append(f"*Total allocation: {total_alloc:.1%}, Implied cash: {cash:.1%}*")
            lines.append("")

    # Reconciliation Actions section
    if "reconciliation" in data:
        deltas = data["reconciliation"].get("deltas", [])
        action_items = [d for d in deltas if d["action"] != "hold"]

        if action_items:
            lines.append("## Actions Required")
            lines.append("")

            # Group by action type
            buys = [d for d in action_items if d["action"] in ("buy", "enter")]
            sells = [d for d in action_items if d["action"] in ("sell", "exit")]

            if buys:
                lines.append("### Buy / Increase Positions")
                lines.append("")
                lines.append("| Symbol | Current | Target | Delta | Est. Trade |")
                lines.append("|--------|---------|--------|-------|------------|")

                for d in buys:
                    symbol = d["symbol"]
                    current = f"{d['current_pct']:.1%}"
                    target = f"{d['target_pct']:.1%}"
                    delta = f"{d['delta_pct']:+.1%}"
                    trade = f"${d['suggested_trade_value']:+,.0f}"
                    lines.append(f"| {symbol} | {current} | {target} | {delta} | {trade} |")

                lines.append("")

            if sells:
                lines.append("### Sell / Reduce Positions")
                lines.append("")
                lines.append("| Symbol | Current | Target | Delta | Est. Trade |")
                lines.append("|--------|---------|--------|-------|------------|")

                for d in sells:
                    symbol = d["symbol"]
                    current = f"{d['current_pct']:.1%}"
                    target = f"{d['target_pct']:.1%}"
                    delta = f"{d['delta_pct']:+.1%}"
                    trade = f"${d['suggested_trade_value']:+,.0f}"
                    action_note = " ⚠️ FULL EXIT" if d["action"] == "exit" else ""
                    lines.append(f"| {symbol} | {current} | {target} | {delta} | {trade} |{action_note}")

                lines.append("")

        # Show aligned positions
        holds = [d for d in deltas if d["action"] == "hold"]
        if holds:
            lines.append("### Aligned Positions (No Action)")
            lines.append("")
            lines.append("| Symbol | Current | Target |")
            lines.append("|--------|---------|--------|")

            for d in holds:
                symbol = d["symbol"]
                current = f"{d['current_pct']:.1%}"
                target = f"{d['target_pct']:.1%}"
                lines.append(f"| {symbol} | {current} | {target} |")

            lines.append("")

    # Proposed Orders section
    if "orders" in data:
        orders = data["orders"].get("orders", [])
        if orders:
            lines.append("## Proposed Orders")
            lines.append("")
            lines.append("> ⚠️ **REVIEW ONLY** - Order execution is disabled in MVP")
            lines.append("")
            lines.append("| # | Side | Symbol | Amount | Rationale |")
            lines.append("|---|------|--------|--------|-----------|")

            for o in orders:
                priority = o["priority"]
                side = o["side"].upper()
                symbol = o["symbol"]
                amount = f"${o['notional']:,.0f}"
                rationale = o["rationale"][:50] + "..." if len(o["rationale"]) > 50 else o["rationale"]
                lines.append(f"| {priority} | {side} | {symbol} | {amount} | {rationale} |")

            lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append("*This report is for informational purposes only. Always verify data before executing trades.*")

    return "\n".join(lines)


def save_report() -> str:
    """Generate and save markdown report"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report = generate_markdown_report()

    # Save with timestamp
    timestamp = datetime.now().strftime("%Y%m%d")
    output_path = REPORTS_DIR / f"reconciliation-{timestamp}.md"

    with open(output_path, "w") as f:
        f.write(report)

    # Also save as latest
    latest_path = REPORTS_DIR / "reconciliation-latest.md"
    with open(latest_path, "w") as f:
        f.write(report)

    print(f"Saved report to {output_path}")
    print(f"Also saved to {latest_path}")

    return str(output_path)


def run_report_generation() -> str:
    """Main entry point"""
    path = save_report()

    # Print preview
    print("\n" + "=" * 60)
    print("REPORT PREVIEW")
    print("=" * 60)
    print(generate_markdown_report()[:2000])
    print("\n... (truncated)")
    print("=" * 60)

    return path


if __name__ == "__main__":
    run_report_generation()
