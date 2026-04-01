#!/usr/bin/env python3
"""
Test script for the signal processing pipeline.

This script tests the full signal processing flow:
1. Creates a signal from a trigger event
2. Fetches portfolio data via the Bravos adapter
3. Interprets into a portfolio intent
4. Validates the intent

Usage:
    # First, ensure browser-worker is running:
    cd browser-worker && uvicorn main:app --host 0.0.0.0 --port 8001 --reload

    # Then run this test:
    python scripts/test-signal-processing.py

    # Or test with environment override:
    BROWSER_WORKER_URL=https://your-service.run.app python scripts/test-signal-processing.py
"""

import asyncio
import os
import sys
from pathlib import Path

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


def print_allocation_table(allocations):
    """Print allocations as a formatted table."""
    print("   " + "-" * 55)
    print(f"   {'Symbol':<8} {'Weight':>8} {'Side':<6} {'Category':<12} {'Name'}")
    print("   " + "-" * 55)

    for alloc in allocations:
        weight_pct = f"{alloc.target_weight * 100:.1f}%"
        name = (alloc.asset_name or "")[:20]
        category = (alloc.category or "")[:10]
        print(f"   {alloc.symbol:<8} {weight_pct:>8} {alloc.side:<6} {category:<12} {name}")

    print("   " + "-" * 55)


async def test_signal_processor():
    """Test the full signal processing pipeline."""
    print_section("SIGNAL PROCESSING PIPELINE TEST")

    from src.adapters.base import AdapterError
    from src.signals.processor import SignalProcessor

    processor = SignalProcessor()

    try:
        # First, check adapter health
        adapter = processor.get_adapter("bravos")
        if adapter is None:
            print("\n❌ No adapter found for 'bravos'")
            return False

        health = await adapter.check_health()
        print(f"\nAdapter health: {health}")

        if not health.get("healthy"):
            print(f"\n⚠️ Adapter not healthy: {health.get('status')}")
            if health.get("status") == "needs_auth":
                print("   Run 'npm run init-session' to authenticate with Bravos")
            return False

        # Process a test signal
        print("\nProcessing test signal...")
        result = await processor.process_bravos_email(
            email_message_id="test_signal_001",
            email_payload={
                "trigger_type": "test",
                "test_run": True,
            },
        )

        print_section("PROCESSING RESULT")

        print(f"Success: {result.success}")
        print(f"Processing time: {result.processing_time_ms}ms")
        print(f"Fetch time: {result.fetch_time_ms}ms")

        if result.error:
            print(f"\n❌ Error: {result.error}")
            print(f"Error type: {result.error_type}")
            return False

        # Signal info
        print_section("SIGNAL")
        print(f"Signal ID: {result.signal.id}")
        print(f"Sleeve ID: {result.signal.sleeve_id}")
        print(f"Source Event ID: {result.signal.source_event_id}")
        print(f"Event Type: {result.signal.event_type}")
        print(f"Status: {result.signal.status}")
        print(f"Detected At: {result.signal.detected_at}")

        # Intent info
        if result.intent:
            print_section("PORTFOLIO INTENT")
            print(f"Intent ID: {result.intent.id}")
            print(f"Intent Type: {result.intent.intent_type}")
            print(f"Confidence: {result.intent.confidence:.0%}")
            print(f"Positions: {result.intent.position_count}")
            print(f"Total Weight: {result.intent.total_weight:.1%}")
            print(f"Requires Review: {result.intent.requires_review}")

            if result.intent.review_reason:
                print(f"Review Reason: {result.intent.review_reason}")

            print("\nTarget Allocations:")
            print_allocation_table(result.intent.target_allocations)

        # Validation info
        print_section("VALIDATION")
        print(f"Passed: {result.validation_passed}")

        if result.validation_issues:
            print("Issues:")
            for issue in result.validation_issues:
                print(f"  - {issue}")
        else:
            print("No issues found")

        print_section("SUMMARY")
        if result.success and result.validation_passed:
            print("✅ Signal processing PASSED - Ready for reconciliation")
        elif result.success and not result.validation_passed:
            print("⚠️ Signal processing completed but validation FAILED")
            print("   Intent flagged for manual review")
        else:
            print("❌ Signal processing FAILED")

        return result.success

    except Exception as e:
        print(f"\n❌ Test FAILED with exception: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        await processor.close()


async def test_intent_interpreter_standalone():
    """Test the intent interpreter with mock data."""
    print_section("INTENT INTERPRETER TEST (Standalone)")

    from uuid import uuid4

    from src.adapters.base import Allocation, PortfolioSnapshot
    from src.intent.interpreter import IntentInterpreter

    # Create mock snapshot
    snapshot = PortfolioSnapshot(
        sleeve_name="test_sleeve",
        allocations=[
            Allocation(
                symbol="AAPL",
                target_weight=0.25,
                side="long",
                raw_weight=25,
                asset_name="Apple Inc.",
                category="Technology",
            ),
            Allocation(
                symbol="GOOGL",
                target_weight=0.25,
                side="long",
                raw_weight=25,
                asset_name="Alphabet Inc.",
                category="Technology",
            ),
            Allocation(
                symbol="MSFT",
                target_weight=0.25,
                side="long",
                raw_weight=25,
                asset_name="Microsoft Corp.",
                category="Technology",
            ),
            Allocation(
                symbol="AMZN",
                target_weight=0.25,
                side="long",
                raw_weight=25,
                asset_name="Amazon.com Inc.",
                category="Consumer",
            ),
        ],
        scraped_at="2024-01-15T10:30:00Z",
        last_updated="2024-01-15T10:00:00Z",
        total_positions=4,
        latency_ms=1500,
        cold_start=False,
    )

    interpreter = IntentInterpreter()

    signal_id = uuid4()
    sleeve_id = uuid4()

    intent = interpreter.interpret(
        signal_id=signal_id,
        sleeve_id=sleeve_id,
        snapshot=snapshot,
    )

    print(f"Intent ID: {intent.id}")
    print(f"Confidence: {intent.confidence:.0%}")
    print(f"Positions: {intent.position_count}")
    print(f"Total Weight: {intent.total_weight:.1%}")
    print(f"Intent Type: {intent.intent_type}")

    print("\nTarget Allocations:")
    print_allocation_table(intent.target_allocations)

    # Weights should sum to 1.0
    if abs(intent.total_weight - 1.0) < 0.01:
        print("\n✅ Intent interpreter PASSED")
        return True
    else:
        print(f"\n❌ Intent interpreter FAILED - weights sum to {intent.total_weight}")
        return False


async def test_validators():
    """Test the validation pipeline with various scenarios."""
    print_section("VALIDATION PIPELINE TEST")

    from uuid import uuid4

    from src.intent.validators import IntentValidationPipeline
    from src.signals.models import IntentType, PortfolioIntent, TargetAllocation

    pipeline = IntentValidationPipeline()

    # Test cases
    test_cases = [
        {
            "name": "Valid portfolio",
            "allocations": [
                TargetAllocation("AAPL", 0.5, "long"),
                TargetAllocation("GOOGL", 0.5, "long"),
            ],
            "expected_pass": True,
        },
        {
            "name": "Weights don't sum to 1.0",
            "allocations": [
                TargetAllocation("AAPL", 0.3, "long"),
                TargetAllocation("GOOGL", 0.3, "long"),
            ],
            "expected_pass": False,
        },
        {
            "name": "Empty portfolio",
            "allocations": [],
            "expected_pass": False,
        },
        {
            "name": "Duplicate symbols",
            "allocations": [
                TargetAllocation("AAPL", 0.5, "long"),
                TargetAllocation("AAPL", 0.5, "long"),
            ],
            "expected_pass": False,
        },
        {
            "name": "Oversized position",
            "allocations": [
                TargetAllocation("AAPL", 0.8, "long"),
                TargetAllocation("GOOGL", 0.2, "long"),
            ],
            "expected_pass": True,  # Warning only, not error
        },
    ]

    all_passed = True

    for case in test_cases:
        intent = PortfolioIntent.create(
            signal_id=uuid4(),
            sleeve_id=uuid4(),
            target_allocations=case["allocations"],
            intent_type=IntentType.FULL_REBALANCE,
            confidence=1.0,
        )

        passed, issues = pipeline.validate_and_flag(intent)

        status = "✅" if (passed == case["expected_pass"]) else "❌"
        result = "PASS" if passed else "FAIL"

        print(f"\n{status} {case['name']}: {result}")

        if issues:
            for issue in issues:
                print(f"   - {issue}")

        if passed != case["expected_pass"]:
            all_passed = False
            print(f"   Expected: {'PASS' if case['expected_pass'] else 'FAIL'}")

    print_section("VALIDATION SUMMARY")
    if all_passed:
        print("✅ All validation tests PASSED")
    else:
        print("❌ Some validation tests FAILED")

    return all_passed


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("SIGNAL PROCESSING TESTS")
    print("=" * 60)

    browser_worker_url = os.getenv("BROWSER_WORKER_URL", "http://localhost:8001")
    print(f"\nBrowser worker URL: {browser_worker_url}")
    print("(Set BROWSER_WORKER_URL env var to test against a different endpoint)")

    results = []

    # Test 1: Intent interpreter (no external dependencies)
    results.append(("Intent Interpreter", await test_intent_interpreter_standalone()))

    # Test 2: Validators (no external dependencies)
    results.append(("Validators", await test_validators()))

    # Test 3: Full pipeline (requires browser-worker)
    print("\n" + "=" * 60)
    print("NOTE: The following test requires browser-worker to be running")
    print("=" * 60)

    try:
        results.append(("Signal Processor", await test_signal_processor()))
    except Exception as e:
        print(f"\n⚠️ Signal processor test skipped: {e}")
        results.append(("Signal Processor", None))

    # Summary
    print_section("TEST SUMMARY")

    passed = 0
    failed = 0
    skipped = 0

    for name, result in results:
        if result is True:
            status = "✅ PASS"
            passed += 1
        elif result is False:
            status = "❌ FAIL"
            failed += 1
        else:
            status = "⏭️ SKIPPED"
            skipped += 1

        print(f"  {name}: {status}")

    print(f"\nTotal: {passed} passed, {failed} failed, {skipped} skipped")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
