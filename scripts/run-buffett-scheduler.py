#!/usr/bin/env python3
"""
Buffett sleeve scheduler - auto-detects new 13F filings.

This script can run in two modes:
1. One-shot mode: Check once and exit (for cron)
2. Continuous mode: Run in a loop with configurable interval

The scheduler:
1. Checks SEC EDGAR for new Berkshire 13F filings
2. If new filing detected, processes it and sends Telegram approval
3. Tracks state to avoid reprocessing

Usage:
    # One-shot mode (for cron)
    python scripts/run-buffett-scheduler.py

    # Continuous mode (for background process)
    python scripts/run-buffett-scheduler.py --continuous

    # With custom interval (in hours)
    python scripts/run-buffett-scheduler.py --continuous --interval 6

    # Dry run (check but don't send approval)
    python scripts/run-buffett-scheduler.py --dry-run

Cron example (check twice daily at 9am and 5pm):
    0 9,17 * * * cd /path/to/investing && python scripts/run-buffett-scheduler.py

13F Filing Schedule:
    Q1 (Mar 31) -> Filed by mid-May
    Q2 (Jun 30) -> Filed by mid-August
    Q3 (Sep 30) -> Filed by mid-November
    Q4 (Dec 31) -> Filed by mid-February
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment
from dotenv import load_dotenv

load_dotenv()


def print_header(text: str):
    """Print a header line."""
    print("=" * 60)
    print(text)
    print("=" * 60)


async def run_check(dry_run: bool = False, force: bool = False) -> bool:
    """
    Run a single filing check.

    Returns:
        True if successful (regardless of whether new filing found)
    """
    from src.signals.buffett_processor import check_and_process_buffett

    now = datetime.now(timezone.utc)
    print(f"\n[{now.strftime('%Y-%m-%d %H:%M UTC')}] Checking for new 13F filing...")

    try:
        result = await check_and_process_buffett(force=force, dry_run=dry_run)

        if not result.success:
            print(f"  ERROR: {result.error}")
            return False

        if not result.new_filing:
            print(f"  No new filing (current: {result.accession_number})")
            return True

        # New filing found
        print(f"  NEW FILING DETECTED!")
        print(f"  Accession: {result.accession_number}")
        print(f"  Report Date: {result.report_date}")
        print(f"  Trades: {result.trade_count}")
        print(f"  Total Buy: ${result.total_buy:,.0f}")
        print(f"  Total Sell: ${result.total_sell:,.0f}")

        if dry_run:
            print(f"  [DRY RUN - no approval sent]")
        elif result.approval_sent:
            print(f"  Approval request sent to Telegram")
        else:
            print(f"  No trades needed")

        return True

    except Exception as e:
        print(f"  EXCEPTION: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


async def run_continuous(
    interval_hours: float = 12,
    dry_run: bool = False,
):
    """
    Run continuous checking loop.

    Args:
        interval_hours: Hours between checks
        dry_run: If True, don't send approval requests
    """
    print_header("Buffett Scheduler (Continuous Mode)")
    print(f"Interval: {interval_hours} hours")
    print(f"Dry Run: {dry_run}")
    print(f"Press Ctrl+C to stop")
    print()

    interval_seconds = interval_hours * 3600

    while True:
        await run_check(dry_run=dry_run)

        next_check = datetime.now(timezone.utc).timestamp() + interval_seconds
        next_check_str = datetime.fromtimestamp(next_check, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        print(f"  Next check at: {next_check_str}")

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            print("\nScheduler stopped.")
            break


async def main():
    parser = argparse.ArgumentParser(
        description="Buffett sleeve 13F auto-detection scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # One-shot check (for cron)
    python scripts/run-buffett-scheduler.py

    # Continuous mode, check every 12 hours
    python scripts/run-buffett-scheduler.py --continuous

    # Continuous mode, check every 6 hours
    python scripts/run-buffett-scheduler.py --continuous --interval 6

    # Dry run (check but don't act)
    python scripts/run-buffett-scheduler.py --dry-run
        """,
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run continuously instead of one-shot",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=12,
        help="Hours between checks in continuous mode (default: 12)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check for filings but don't send approval requests",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reprocess even if already processed (one-shot mode only)",
    )

    args = parser.parse_args()

    if args.continuous:
        await run_continuous(
            interval_hours=args.interval,
            dry_run=args.dry_run,
        )
    else:
        print_header("Buffett Scheduler (One-Shot)")
        print(f"Dry Run: {args.dry_run}")
        print(f"Force: {args.force}")

        success = await run_check(dry_run=args.dry_run, force=args.force)
        return 0 if success else 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
