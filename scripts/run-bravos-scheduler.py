#!/usr/bin/env python3
"""
Bravos sleeve scheduler - auto-detects portfolio update emails.

This script can run in two modes:
1. One-shot mode: Check once and exit (for cron)
2. Continuous mode: Run in a loop with configurable interval

The scheduler:
1. Checks Gmail for new Bravos portfolio update emails
2. If new email detected, triggers the reconciliation pipeline
3. Sends proposed trades to Telegram for approval
4. Tracks state to avoid reprocessing

Usage:
    # One-shot mode (for cron)
    python scripts/run-bravos-scheduler.py

    # Continuous mode (for background process)
    python scripts/run-bravos-scheduler.py --continuous

    # With custom interval (in minutes)
    python scripts/run-bravos-scheduler.py --continuous --interval 30

    # Dry run (check but don't send approval)
    python scripts/run-bravos-scheduler.py --dry-run

    # Skip scraping (use existing data)
    python scripts/run-bravos-scheduler.py --skip-scrape

Cron example (check every 3 hours):
    0 */3 * * * cd /path/to/investing && python scripts/run-bravos-scheduler.py

Note: Requires Gmail API credentials configured in .env
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


async def run_check(
    dry_run: bool = False,
    force: bool = False,
    skip_scrape: bool = False,
) -> bool:
    """
    Run a single email check.

    Returns:
        True if successful (regardless of whether new email found)
    """
    from src.signals.bravos_processor import check_and_process_bravos

    now = datetime.now(timezone.utc)
    print(f"\n[{now.strftime('%Y-%m-%d %H:%M UTC')}] Checking for new Bravos email...")

    try:
        result = await check_and_process_bravos(
            force=force,
            dry_run=dry_run,
            skip_scrape=skip_scrape,
        )

        if not result.success:
            print(f"  ERROR: {result.error}")
            return False

        if not result.new_email and not force and not skip_scrape:
            print(f"  No new email detected")
            return True

        # New email found or forced processing
        print(f"  PROCESSING TRIGGERED!")
        if result.message_id:
            print(f"  Email: {result.subject or '(no subject)'}")
        print(f"  Trades: {result.trade_count}")
        print(f"  Total Buy: ${result.total_buy:,.0f}")
        print(f"  Total Sell: ${result.total_sell:,.0f}")

        if dry_run:
            print(f"  [DRY RUN - no approval sent]")
        elif result.approval_sent:
            print(f"  Approval request sent to Telegram")
        elif result.trade_count == 0:
            print(f"  No trades needed")
        else:
            print(f"  Approval not sent (check logs)")

        return True

    except Exception as e:
        print(f"  EXCEPTION: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


async def run_continuous(
    interval_minutes: float = 60,
    dry_run: bool = False,
):
    """
    Run continuous checking loop.

    Args:
        interval_minutes: Minutes between checks
        dry_run: If True, don't send approval requests
    """
    print_header("Bravos Scheduler (Continuous Mode)")
    print(f"Interval: {interval_minutes} minutes")
    print(f"Dry Run: {dry_run}")
    print(f"Press Ctrl+C to stop")
    print()

    interval_seconds = interval_minutes * 60

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
        description="Bravos sleeve email auto-detection scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # One-shot check (for cron)
    python scripts/run-bravos-scheduler.py

    # Continuous mode, check every hour
    python scripts/run-bravos-scheduler.py --continuous

    # Continuous mode, check every 30 minutes
    python scripts/run-bravos-scheduler.py --continuous --interval 30

    # Dry run (check but don't act)
    python scripts/run-bravos-scheduler.py --dry-run

    # Force reprocess (ignore email state)
    python scripts/run-bravos-scheduler.py --force

    # Skip scraping (use existing reconciliation data)
    python scripts/run-bravos-scheduler.py --skip-scrape
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
        default=60,
        help="Minutes between checks in continuous mode (default: 60)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check for emails but don't send approval requests",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reprocess even if no new email (one-shot mode only)",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip scraping, use existing reconciliation data",
    )

    args = parser.parse_args()

    if args.continuous:
        await run_continuous(
            interval_minutes=args.interval,
            dry_run=args.dry_run,
        )
    else:
        print_header("Bravos Scheduler (One-Shot)")
        print(f"Dry Run: {args.dry_run}")
        print(f"Force: {args.force}")
        print(f"Skip Scrape: {args.skip_scrape}")

        success = await run_check(
            dry_run=args.dry_run,
            force=args.force,
            skip_scrape=args.skip_scrape,
        )
        return 0 if success else 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
