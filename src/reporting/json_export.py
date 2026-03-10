"""
Export data snapshots to JSON format.
"""

import json
from datetime import datetime
from pathlib import Path
import shutil


PROCESSED_DATA_DIR = Path("data/processed")
REPORTS_DIR = Path("data/reports")


def create_snapshot() -> str:
    """
    Create a timestamped snapshot of all processed data.
    Useful for tracking changes over time.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_dir = REPORTS_DIR / f"snapshot-{timestamp}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    files_to_snapshot = [
        "ideas.json",
        "target_allocations.json",
        "robinhood_holdings.json",
        "reconciliation.json",
        "proposed_orders.json",
    ]

    copied = []

    for filename in files_to_snapshot:
        source = PROCESSED_DATA_DIR / filename
        if source.exists():
            dest = snapshot_dir / filename
            shutil.copy2(source, dest)
            copied.append(filename)

    # Create manifest
    manifest = {
        "snapshot_time": datetime.now().isoformat(),
        "files": copied,
    }

    manifest_path = snapshot_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Created snapshot at {snapshot_dir}")
    print(f"Files included: {', '.join(copied)}")

    return str(snapshot_dir)


def export_combined_report() -> str:
    """
    Create a single JSON file with all reconciliation data combined.
    Useful for importing into other tools or dashboards.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "generated_at": datetime.now().isoformat(),
        "ideas": None,
        "target_allocations": None,
        "holdings": None,
        "reconciliation": None,
        "proposed_orders": None,
    }

    # Load each data file
    files_map = {
        "ideas": "ideas.json",
        "target_allocations": "target_allocations.json",
        "holdings": "robinhood_holdings.json",
        "reconciliation": "reconciliation.json",
        "proposed_orders": "proposed_orders.json",
    }

    for key, filename in files_map.items():
        filepath = PROCESSED_DATA_DIR / filename
        if filepath.exists():
            with open(filepath) as f:
                report[key] = json.load(f)

    # Save combined report
    output_path = REPORTS_DIR / "combined_report.json"
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Created combined report at {output_path}")
    return str(output_path)


if __name__ == "__main__":
    export_combined_report()
    create_snapshot()
