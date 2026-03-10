"""
Export data to CSV format.
"""

import csv
import json
from datetime import datetime
from pathlib import Path


PROCESSED_DATA_DIR = Path("data/processed")
REPORTS_DIR = Path("data/reports")


def export_ideas_to_csv() -> str | None:
    """Export normalized ideas to CSV"""
    ideas_path = PROCESSED_DATA_DIR / "ideas.json"

    if not ideas_path.exists():
        print("No ideas data found")
        return None

    with open(ideas_path) as f:
        data = json.load(f)

    ideas = data.get("ideas", [])

    if not ideas:
        print("No ideas to export")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REPORTS_DIR / "ideas.csv"

    fieldnames = [
        "idea_id",
        "date",
        "symbol",
        "side",
        "entry_price",
        "target_price",
        "stop_loss",
        "relative_weight",
        "status",
        "notes",
        "source_url",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for idea in ideas:
            # Convert relative_weight to percentage string
            row = idea.copy()
            if row.get("relative_weight"):
                row["relative_weight"] = f"{row['relative_weight']:.2%}"
            writer.writerow(row)

    print(f"Exported {len(ideas)} ideas to {output_path}")
    return str(output_path)


def export_allocations_to_csv() -> str | None:
    """Export target allocations to CSV"""
    alloc_path = PROCESSED_DATA_DIR / "target_allocations.json"

    if not alloc_path.exists():
        print("No allocations data found")
        return None

    with open(alloc_path) as f:
        data = json.load(f)

    allocations = data.get("allocations", [])

    if not allocations:
        print("No allocations to export")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REPORTS_DIR / "target_allocations.csv"

    fieldnames = [
        "symbol",
        "target_pct",
        "side",
        "idea_count",
        "last_updated",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for alloc in allocations:
            row = {
                "symbol": alloc["symbol"],
                "target_pct": f"{alloc['target_pct']:.2%}",
                "side": alloc["side"],
                "idea_count": alloc["idea_count"],
                "last_updated": alloc["last_updated"],
            }
            writer.writerow(row)

    print(f"Exported {len(allocations)} allocations to {output_path}")
    return str(output_path)


def export_holdings_to_csv() -> str | None:
    """Export Robinhood holdings to CSV"""
    holdings_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"

    if not holdings_path.exists():
        print("No holdings data found")
        return None

    with open(holdings_path) as f:
        data = json.load(f)

    holdings = data.get("holdings", [])

    if not holdings:
        print("No holdings to export")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REPORTS_DIR / "robinhood_holdings.csv"

    fieldnames = [
        "symbol",
        "quantity",
        "avg_cost",
        "current_price",
        "market_value",
        "current_pct",
        "unrealized_pl",
        "unrealized_pl_pct",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for holding in holdings:
            row = holding.copy()
            row["current_pct"] = f"{row['current_pct']:.2%}"
            row["unrealized_pl_pct"] = f"{row['unrealized_pl_pct']:+.2f}%"
            writer.writerow(row)

    print(f"Exported {len(holdings)} holdings to {output_path}")
    return str(output_path)


def export_reconciliation_to_csv() -> str | None:
    """Export reconciliation deltas to CSV"""
    recon_path = PROCESSED_DATA_DIR / "reconciliation.json"

    if not recon_path.exists():
        print("No reconciliation data found")
        return None

    with open(recon_path) as f:
        data = json.load(f)

    deltas = data.get("deltas", [])

    if not deltas:
        print("No deltas to export")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REPORTS_DIR / "reconciliation.csv"

    fieldnames = [
        "symbol",
        "action",
        "current_pct",
        "target_pct",
        "delta_pct",
        "current_value",
        "suggested_trade_value",
        "notes",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for delta in deltas:
            row = {
                "symbol": delta["symbol"],
                "action": delta["action"].upper(),
                "current_pct": f"{delta['current_pct']:.2%}",
                "target_pct": f"{delta['target_pct']:.2%}",
                "delta_pct": f"{delta['delta_pct']:+.2%}",
                "current_value": f"${delta['current_value']:,.2f}",
                "suggested_trade_value": f"${delta['suggested_trade_value']:+,.2f}",
                "notes": delta["notes"],
            }
            writer.writerow(row)

    print(f"Exported {len(deltas)} deltas to {output_path}")
    return str(output_path)


def export_all_to_csv() -> list[str]:
    """Export all data to CSV files"""
    exported = []

    result = export_ideas_to_csv()
    if result:
        exported.append(result)

    result = export_allocations_to_csv()
    if result:
        exported.append(result)

    result = export_holdings_to_csv()
    if result:
        exported.append(result)

    result = export_reconciliation_to_csv()
    if result:
        exported.append(result)

    return exported


if __name__ == "__main__":
    export_all_to_csv()
