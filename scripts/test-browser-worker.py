#!/usr/bin/env python3
"""
Test script for browser-worker integration.

This script tests the communication between the orchestrator and browser-worker
services. Run both services locally before running this test.

Usage:
    # Terminal 1: Start browser-worker
    cd browser-worker && uvicorn main:app --host 0.0.0.0 --port 8001 --reload

    # Terminal 2: Run this test
    python scripts/test-browser-worker.py

    # Or test against deployed services
    BROWSER_WORKER_URL=https://your-service.run.app python scripts/test-browser-worker.py
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


async def test_health_check():
    """Test browser-worker health endpoint."""
    print("\n" + "=" * 60)
    print("TEST: Health Check")
    print("=" * 60)

    from src.adapters.http_client import BrowserWorkerClient

    client = BrowserWorkerClient()

    try:
        health = await client.health_check()
        print(f"Status: {health.get('status')}")
        print(f"Node available: {health.get('node_available')}")
        print(f"Session exists: {health.get('session_exists')}")
        print(f"Latency info: {health.get('latency_info')}")

        if health.get("status") == "healthy":
            print("\n✅ Health check PASSED")
            return True
        elif health.get("status") == "needs_auth":
            print("\n⚠️ Session needs initialization - run 'npm run init-session'")
            return False
        else:
            print(f"\n❌ Health check FAILED: {health}")
            return False

    except Exception as e:
        print(f"\n❌ Health check FAILED with exception: {e}")
        return False

    finally:
        await client.close()


async def test_bravos_adapter():
    """Test the Bravos adapter end-to-end."""
    print("\n" + "=" * 60)
    print("TEST: Bravos Adapter")
    print("=" * 60)

    from src.adapters.base import AdapterError
    from src.adapters.bravos_web import BravosWebAdapter

    adapter = BravosWebAdapter()

    try:
        # Check adapter health first
        health = await adapter.check_health()
        print(f"Adapter health: {health}")

        if not health.get("healthy"):
            print(f"\n⚠️ Adapter not healthy: {health.get('status')}")
            if health.get("status") == "needs_auth":
                print("   Run 'npm run init-session' to authenticate with Bravos")
            return False

        # Try to fetch portfolio
        print("\nFetching Bravos portfolio...")
        result = await adapter.fetch_portfolio()

        if isinstance(result, AdapterError):
            print(f"\n❌ Fetch FAILED: {result.error_type}")
            print(f"   Message: {result.message}")
            print(f"   Recoverable: {result.recoverable}")
            return False

        # Success - print results
        print(f"\n✅ Fetch SUCCEEDED")
        print(f"   Sleeve: {result.sleeve_name}")
        print(f"   Positions: {result.total_positions}")
        print(f"   Latency: {result.latency_ms}ms")
        print(f"   Cold start: {result.cold_start}")
        print(f"   Scraped at: {result.scraped_at}")
        print(f"   Last updated: {result.last_updated}")

        print("\n   Allocations:")
        print("   " + "-" * 50)
        print(f"   {'Symbol':<8} {'Weight':>8} {'Side':<6} {'Name'}")
        print("   " + "-" * 50)

        for alloc in result.allocations:
            weight_pct = f"{alloc.target_weight * 100:.1f}%"
            name = (alloc.asset_name or "")[:30]
            print(f"   {alloc.symbol:<8} {weight_pct:>8} {alloc.side:<6} {name}")

        # Verify allocations sum to ~1.0
        total_weight = sum(a.target_weight for a in result.allocations)
        print("   " + "-" * 50)
        print(f"   Total weight: {total_weight * 100:.1f}%")

        if abs(total_weight - 1.0) > 0.01:
            print(f"\n⚠️ Warning: Total weight is {total_weight:.2f}, expected ~1.0")

        return True

    except Exception as e:
        print(f"\n❌ Test FAILED with exception: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        await adapter.close()


async def test_scrape_directly():
    """Test scraping directly via HTTP client."""
    print("\n" + "=" * 60)
    print("TEST: Direct Scrape Call")
    print("=" * 60)

    from src.adapters.http_client import BrowserWorkerClient

    client = BrowserWorkerClient()

    try:
        print("Calling /scrape/bravos...")
        response = await client.scrape_bravos()

        print(f"Success: {response.get('success')}")
        print(f"Latency: {response.get('latency_ms')}ms")
        print(f"Cold start: {response.get('cold_start')}")
        print(f"Positions: {response.get('total_positions')}")

        if response.get("error"):
            print(f"Error: {response.get('error')}")
            print(f"Error type: {response.get('error_type')}")

        if response.get("success"):
            print("\n✅ Direct scrape PASSED")
            return True
        else:
            print("\n❌ Direct scrape FAILED")
            return False

    except Exception as e:
        print(f"\n❌ Direct scrape FAILED with exception: {e}")
        return False

    finally:
        await client.close()


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("BROWSER-WORKER INTEGRATION TESTS")
    print("=" * 60)

    browser_worker_url = os.getenv("BROWSER_WORKER_URL", "http://localhost:8001")
    print(f"\nBrowser worker URL: {browser_worker_url}")
    print("(Set BROWSER_WORKER_URL env var to test against a different endpoint)")

    results = []

    # Test 1: Health check
    results.append(("Health Check", await test_health_check()))

    # Only continue if health check passes
    if results[0][1]:
        # Test 2: Direct scrape
        results.append(("Direct Scrape", await test_scrape_directly()))

        # Test 3: Adapter
        results.append(("Bravos Adapter", await test_bravos_adapter()))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed = 0
    failed = 0
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {name}: {status}")
        if result:
            passed += 1
        else:
            failed += 1

    print(f"\nTotal: {passed} passed, {failed} failed")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
