#!/usr/bin/env python3
"""
Test script for trade execution system.

This script tests:
1. Safety checks (dry run, max notional, market hours)
2. Idempotency tracking
3. Broker adapter (connection, quotes, positions)
4. Full execution flow (in dry-run mode)

IMPORTANT: By default, this runs in DRY_RUN mode.
No real trades will be executed unless you explicitly
set DRY_RUN=false in your environment.

Usage:
    # Run tests (safe - dry run mode)
    python scripts/test-execution.py

    # Run with Robinhood connection test
    python scripts/test-execution.py --test-broker
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables
from dotenv import load_dotenv

load_dotenv()


def print_section(title: str):
    """Print a section header."""
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


async def test_safety_checks():
    """Test the safety checking system."""
    print_section("SAFETY CHECKS")

    from src.brokers.base import AccountInfo, OrderRequest, OrderSide
    from src.execution.safety import SafetyChecker

    # Create checker with test settings
    checker = SafetyChecker(
        max_trade_notional=500.0,
        max_portfolio_change_pct=0.05,
        market_hours_only=False,  # Disable for testing
        dry_run=True,
    )

    print(f"Safety configuration:")
    print(f"  Max trade notional: ${checker.max_trade_notional}")
    print(f"  Max portfolio change: {checker.max_portfolio_change_pct:.0%}")
    print(f"  Market hours only: {checker.market_hours_only}")
    print(f"  Dry run: {checker.dry_run}")

    # Test case 1: Valid trade (but dry run blocks it)
    print("\nTest 1: Valid trade (dry run mode)")
    request = OrderRequest(
        symbol="AAPL",
        side=OrderSide.BUY,
        notional=100.0,
    )
    report = checker.check_single_trade(request)
    print(f"  Passed: {report.passed}")
    print(f"  Blocked: {len(report.blocked_trades)}")
    for check in report.checks:
        status = "" if check.passed else ""
        print(f"    {status} {check.check_name}: {check.message or 'OK'}")

    # Test case 2: Trade exceeds max notional
    print("\nTest 2: Exceeds max notional")
    checker_no_dry = SafetyChecker(
        max_trade_notional=500.0,
        dry_run=False,
        market_hours_only=False,
    )
    request = OrderRequest(
        symbol="AAPL",
        side=OrderSide.BUY,
        notional=1000.0,  # Exceeds $500 limit
    )
    report = checker_no_dry.check_single_trade(request)
    print(f"  Passed: {report.passed}")
    for check in report.checks:
        if not check.passed:
            print(f"    {check.check_name}: {check.message}")

    # Test case 3: Portfolio impact check
    print("\nTest 3: Portfolio impact check")
    account = AccountInfo(
        portfolio_value=10000.0,
        cash_balance=2000.0,
        buying_power=2000.0,
        positions_count=5,
    )
    request = OrderRequest(
        symbol="AAPL",
        side=OrderSide.BUY,
        notional=1000.0,  # 10% of portfolio
    )
    report = checker_no_dry.check_single_trade(request, account)
    print(f"  Portfolio value: ${account.portfolio_value}")
    print(f"  Trade notional: ${request.notional}")
    print(f"  Impact: {request.notional / account.portfolio_value:.1%}")
    print(f"  Passed: {report.passed}")
    for check in report.checks:
        if "portfolio" in check.check_name:
            print(f"    {check.check_name}: {check.message or 'OK'}")

    print("\n Safety checks test completed")
    return True


async def test_idempotency():
    """Test the idempotency tracking system."""
    print_section("IDEMPOTENCY TRACKING")

    from src.brokers.base import OrderResult, OrderSide, OrderStatus
    from src.execution.idempotency import (
        ExecutionState,
        IdempotencyTracker,
        generate_execution_key,
    )

    tracker = IdempotencyTracker()
    approval_id = uuid4()

    print(f"Approval ID: {approval_id}")

    # Test 1: Generate execution key
    print("\nTest 1: Generate execution key")
    key = generate_execution_key(approval_id, "AAPL", OrderSide.BUY)
    print(f"  Key for AAPL BUY: {key}")

    key2 = generate_execution_key(approval_id, "AAPL", OrderSide.SELL)
    print(f"  Key for AAPL SELL: {key2}")
    print(f"  Keys are different: {key != key2}")

    # Test 2: Create execution record
    print("\nTest 2: Create execution record")
    record, is_new = tracker.get_or_create(
        approval_id=approval_id,
        symbol="AAPL",
        side=OrderSide.BUY,
        notional=100.0,
    )
    print(f"  Record created: {is_new}")
    print(f"  State: {record.state.value}")

    # Test 3: Check safety before execution
    print("\nTest 3: Check if safe to execute")
    is_safe, reason = tracker.is_safe_to_execute(key)
    print(f"  Safe to execute: {is_safe}")
    print(f"  Reason: {reason}")

    # Test 4: Mark as in progress
    print("\nTest 4: Mark in progress")
    success = tracker.mark_in_progress(key)
    print(f"  Marked in progress: {success}")
    record = tracker.get_execution(key)
    print(f"  State: {record.state.value}")

    # Test 5: Check safety again (should fail - in progress)
    print("\nTest 5: Check safety while in progress")
    is_safe, reason = tracker.is_safe_to_execute(key)
    print(f"  Safe to execute: {is_safe}")
    print(f"  Reason: {reason}")

    # Test 6: Update with order result
    print("\nTest 6: Update with order result")
    order_result = OrderResult(
        success=True,
        order_id="TEST-ORDER-123",
        status=OrderStatus.FILLED,
        filled_quantity=5.0,
        filled_price=20.0,
        filled_notional=100.0,
    )
    updated = tracker.update_from_order_result(key, order_result)
    print(f"  State: {updated.state.value}")
    print(f"  Order ID: {updated.broker_order_id}")
    print(f"  Filled: {updated.filled_quantity} @ ${updated.filled_price}")

    # Test 7: Check safety after filled (should fail)
    print("\nTest 7: Check safety after filled")
    is_safe, reason = tracker.is_safe_to_execute(key)
    print(f"  Safe to execute: {is_safe}")
    print(f"  Reason: {reason}")

    # Test 8: Try duplicate (same approval, symbol, side)
    print("\nTest 8: Try duplicate execution")
    record2, is_new2 = tracker.get_or_create(
        approval_id=approval_id,
        symbol="AAPL",
        side=OrderSide.BUY,
        notional=100.0,
    )
    print(f"  Created new record: {is_new2}")
    print(f"  Got existing record: {record2.execution_key == key}")

    print("\n Idempotency test completed")
    return True


async def test_broker_connection(test_live: bool = False):
    """Test broker adapter connection."""
    print_section("BROKER ADAPTER")

    if not test_live:
        print("Skipping live broker test (use --test-broker to enable)")
        return None

    from src.brokers.robinhood import RobinhoodAdapter

    adapter = RobinhoodAdapter()

    print("Connecting to Robinhood...")

    try:
        connected = await adapter.connect()

        if not connected:
            print(" Connection failed")
            return False

        print(" Connected successfully")

        # Get account info
        print("\nFetching account info...")
        account = await adapter.get_account_info()

        if account:
            print(f"  Portfolio value: ${account.portfolio_value:,.2f}")
            print(f"  Cash balance: ${account.cash_balance:,.2f}")
            print(f"  Buying power: ${account.buying_power:,.2f}")
            print(f"  Positions: {account.positions_count}")
        else:
            print("  Failed to get account info")

        # Get positions
        print("\nFetching positions...")
        positions = await adapter.get_positions()

        if positions:
            print(f"  Found {len(positions)} positions:")
            for pos in positions[:5]:
                print(
                    f"    {pos.symbol}: {pos.quantity} shares @ ${pos.current_price:.2f} "
                    f"({pos.weight:.1%})"
                )
            if len(positions) > 5:
                print(f"    ... and {len(positions) - 5} more")
        else:
            print("  No positions found")

        # Get a quote
        print("\nFetching quote for AAPL...")
        quote = await adapter.get_quote("AAPL")

        if quote:
            print(f"  Symbol: {quote.symbol}")
            print(f"  Last: ${quote.last:.2f}")
            print(f"  Bid: ${quote.bid:.2f}" if quote.bid else "  Bid: N/A")
            print(f"  Ask: ${quote.ask:.2f}" if quote.ask else "  Ask: N/A")
        else:
            print("  Failed to get quote")

        await adapter.disconnect()
        print("\n Broker test completed")
        return True

    except Exception as e:
        print(f"\n Broker test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_full_execution_flow():
    """Test the full execution flow in dry-run mode."""
    print_section("FULL EXECUTION FLOW (DRY RUN)")

    from src.execution.executor import TradeExecutor

    # Create executor (will use dry-run from settings)
    executor = TradeExecutor()

    print(f"Dry run mode: {executor.dry_run}")
    print(f"Max trade notional: ${executor.safety_checker.max_trade_notional}")

    if not executor.dry_run:
        print("\n WARNING: Dry run is OFF! Aborting to prevent real trades.")
        print("Set DRY_RUN=true in .env to run this test safely.")
        return False

    # Create test trades
    approval_id = uuid4()
    test_trades = [
        {"symbol": "AAPL", "side": "buy", "notional": 100.0},
        {"symbol": "GOOGL", "side": "buy", "notional": 150.0},
        {"symbol": "MSFT", "side": "sell", "notional": 75.0},
    ]

    print(f"\nApproval ID: {approval_id}")
    print(f"Test trades: {len(test_trades)}")

    for trade in test_trades:
        print(f"  {trade['side'].upper()} {trade['symbol']}: ${trade['notional']}")

    print("\nExecuting trades...")
    report = await executor.execute_approved_trades(
        approval_id=approval_id,
        trades=test_trades,
    )

    print(f"\nExecution Report:")
    print(f"  Success: {report.success}")
    print(f"  Total trades: {report.total_trades}")
    print(f"  Executed: {report.executed}")
    print(f"  Skipped: {report.skipped}")
    print(f"  Failed: {report.failed}")
    print(f"  Dry run: {report.dry_run}")

    if report.results:
        print("\nResults:")
        for result in report.results:
            status = "" if result.success else "" if result.skipped else ""
            reason = result.skip_reason or result.error or "OK"
            print(f"  {status} {result.symbol}: {reason}")

    if report.safety_report:
        print("\nSafety report:")
        for check in report.safety_report.checks:
            status = "" if check.passed else ""
            print(f"  {status} {check.check_name}")

    print("\n Execution flow test completed")
    return True


async def main(test_broker: bool = False):
    """Run all tests."""
    print("\n" + "=" * 60)
    print("TRADE EXECUTION TESTS")
    print("=" * 60)

    # Check environment
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    print(f"\nDRY_RUN mode: {dry_run}")

    if not dry_run:
        print("\n" + "!" * 60)
        print("WARNING: DRY_RUN is OFF!")
        print("Real trades could be executed!")
        print("!" * 60)
        response = input("\nType 'yes' to continue: ")
        if response.lower() != "yes":
            print("Aborted.")
            return False

    results = []

    # Test 1: Safety checks
    results.append(("Safety Checks", await test_safety_checks()))

    # Test 2: Idempotency
    results.append(("Idempotency", await test_idempotency()))

    # Test 3: Broker connection (optional)
    results.append(("Broker Connection", await test_broker_connection(test_broker)))

    # Test 4: Full execution flow
    results.append(("Execution Flow", await test_full_execution_flow()))

    # Summary
    print_section("TEST SUMMARY")

    passed = 0
    failed = 0
    skipped = 0

    for name, result in results:
        if result is True:
            status = "PASS"
            passed += 1
        elif result is False:
            status = "FAIL"
            failed += 1
        else:
            status = "SKIPPED"
            skipped += 1

        print(f"  {name}: {status}")

    print(f"\nTotal: {passed} passed, {failed} failed, {skipped} skipped")

    return failed == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test trade execution system")
    parser.add_argument(
        "--test-broker",
        action="store_true",
        help="Enable live broker connection test (requires Robinhood credentials)",
    )
    args = parser.parse_args()

    success = asyncio.run(main(test_broker=args.test_broker))
    sys.exit(0 if success else 1)
