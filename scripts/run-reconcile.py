#!/usr/bin/env python3
"""
Full reconciliation pipeline.
Fetches Robinhood holdings, runs reconciliation, and generates reports.

Usage: python scripts/run-reconcile.py
   Or: make reconcile
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.parsing.normalize_ideas import run_normalization
from src.parsing.derive_positions import run_derivation
from src.robinhood.holdings import fetch_and_save_holdings
from src.recon.reconcile import run_reconciliation
from src.recon.generate_orders import run_order_generation
from src.reporting.csv_export import export_all_to_csv
from src.reporting.markdown_report import run_report_generation


def main():
    print("=" * 60)
    print("Bravos Trade Reconciliation Pipeline")
    print("=" * 60)
    print()

    # Step 1: Normalize scraped ideas
    print("Step 1: Normalizing scraped ideas...")
    print("-" * 40)
    ideas = run_normalization()
    if not ideas:
        print("No ideas to process. Run 'npm run scrape' first.")
        print()

    # Step 2: Derive target allocations
    print()
    print("Step 2: Deriving target allocations...")
    print("-" * 40)
    allocations = run_derivation()
    if not allocations:
        print("No allocations derived.")
        print()

    # Step 3: Fetch Robinhood holdings
    print()
    print("Step 3: Fetching Robinhood holdings...")
    print("-" * 40)
    holdings = fetch_and_save_holdings()
    if not holdings:
        print("WARNING: No holdings fetched from Robinhood.")
        print("Check RH_USERNAME and RH_PASSWORD in .env")
        print()

    # Step 4: Run reconciliation
    print()
    print("Step 4: Running reconciliation...")
    print("-" * 40)
    recon_result = run_reconciliation()

    # Step 5: Generate proposed orders
    print()
    print("Step 5: Generating proposed orders...")
    print("-" * 40)
    orders = run_order_generation()

    # Step 6: Generate reports
    print()
    print("Step 6: Generating reports...")
    print("-" * 40)
    export_all_to_csv()
    report_path = run_report_generation()

    # Summary
    print()
    print("=" * 60)
    print("Pipeline Complete!")
    print("=" * 60)
    print()
    print("Generated files:")
    print("  - data/processed/ideas.json")
    print("  - data/processed/target_allocations.json")
    print("  - data/processed/robinhood_holdings.json")
    print("  - data/processed/reconciliation.json")
    print("  - data/processed/proposed_orders.json")
    print("  - data/reports/*.csv")
    print(f"  - {report_path}")
    print()
    print("View the markdown report for a summary of actions needed.")
    print()


if __name__ == "__main__":
    main()
