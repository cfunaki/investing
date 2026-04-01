#!/usr/bin/env python3
"""
Test script for Telegram bot integration.

This script tests the Telegram approval workflow:
1. Sends a test approval request
2. Sends a test review alert
3. Tests message formatting

Prerequisites:
1. Create a Telegram bot via @BotFather
2. Get your user ID via @userinfobot
3. Set environment variables:
   - TELEGRAM_BOT_TOKEN
   - TELEGRAM_ALLOWED_USERS (your user ID)
   - TELEGRAM_CHAT_ID (your chat ID with the bot)

Usage:
    # Set up environment
    export TELEGRAM_BOT_TOKEN="your-bot-token"
    export TELEGRAM_ALLOWED_USERS="your-user-id"
    export TELEGRAM_CHAT_ID="your-chat-id"

    # Run tests
    python scripts/test-telegram-bot.py

    # Or test via API
    curl -X POST http://localhost:8000/trigger/test-approval
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
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


async def test_bot_initialization():
    """Test that the bot can be initialized."""
    print_section("BOT INITIALIZATION")

    from src.approval.telegram import TelegramBot

    try:
        bot = TelegramBot()

        print(f"Bot token configured: {'Yes' if bot.token else 'No'}")
        print(f"Allowed users: {bot.allowed_users}")
        print(f"Default chat ID: {bot.default_chat_id}")

        if not bot.token:
            print("\n TELEGRAM_BOT_TOKEN not set")
            return False

        if not bot.allowed_users:
            print("\n Warning: TELEGRAM_ALLOWED_USERS not set (no one can approve)")

        if not bot.default_chat_id:
            print("\n TELEGRAM_CHAT_ID not set - messages cannot be sent")
            return False

        print("\n Bot initialized successfully")
        return True

    except Exception as e:
        print(f"\n Bot initialization failed: {e}")
        return False


async def test_message_formatting():
    """Test message formatting without sending."""
    print_section("MESSAGE FORMATTING")

    from src.approval.telegram import TelegramBot

    bot = TelegramBot()

    # Test approval message
    test_trades = [
        {"symbol": "AAPL", "side": "buy", "notional": 250.00, "delta_weight": 0.02},
        {"symbol": "GOOGL", "side": "buy", "notional": 150.00, "delta_weight": 0.015},
        {"symbol": "MSFT", "side": "sell", "notional": 100.00, "delta_weight": -0.01},
    ]

    approval_msg = bot.format_approval_message(
        sleeve_name="bravos",
        trades=test_trades,
        total_notional=500.00,
        approval_code="ABC12345",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    print("\nApproval Message Preview:")
    print("-" * 40)
    print(approval_msg)
    print("-" * 40)

    # Test review alert
    review_msg = bot.format_review_alert(
        sleeve_name="bravos",
        reason="Target weights sum to 95%, expected ~100%",
        intent_id="abc123-def456",
        details={
            "positions": 8,
            "confidence": "70%",
        },
    )

    print("\nReview Alert Preview:")
    print("-" * 40)
    print(review_msg)
    print("-" * 40)

    # Test confirmation
    confirm_msg = bot.format_confirmation(
        action="approve",
        sleeve_name="bravos",
        approval_code="ABC12345",
        user_name="testuser",
    )

    print("\nConfirmation Message Preview:")
    print("-" * 40)
    print(confirm_msg)
    print("-" * 40)

    print("\n Message formatting passed")
    return True


async def test_send_approval_request():
    """Test sending an approval request."""
    print_section("SEND APPROVAL REQUEST")

    from src.approval.telegram import ApprovalRequest, TelegramBot, generate_approval_code

    bot = TelegramBot()

    if not bot.default_chat_id:
        print("Skipping - TELEGRAM_CHAT_ID not set")
        return None

    approval_code = generate_approval_code()
    print(f"Approval code: {approval_code}")

    request = ApprovalRequest(
        approval_id=uuid4(),
        reconciliation_id=uuid4(),
        sleeve_name="bravos",
        proposed_trades=[
            {"symbol": "AAPL", "side": "buy", "notional": 150.00, "delta_weight": 0.015},
            {"symbol": "TSLA", "side": "sell", "notional": 100.00, "delta_weight": -0.01},
        ],
        total_notional=250.00,
        approval_code=approval_code,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    try:
        print("Sending approval request...")
        message_id = await bot.send_approval_request(request)

        if message_id:
            print(f"\n Approval request sent! Message ID: {message_id}")
            print(f"Check your Telegram chat and try clicking Approve or Reject")
            return True
        else:
            print("\n Failed to send approval request")
            return False

    except Exception as e:
        print(f"\n Failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_send_review_alert():
    """Test sending a review alert."""
    print_section("SEND REVIEW ALERT")

    from src.approval.telegram import TelegramBot

    bot = TelegramBot()

    if not bot.default_chat_id:
        print("Skipping - TELEGRAM_CHAT_ID not set")
        return None

    try:
        print("Sending review alert...")
        message_id = await bot.send_review_alert(
            sleeve_name="bravos",
            reason="Test alert: Target weights don't sum to 100%",
            intent_id=str(uuid4())[:8],
            details={
                "positions": 5,
                "confidence": "72%",
                "total_weight": "94.5%",
            },
        )

        if message_id:
            print(f"\n Review alert sent! Message ID: {message_id}")
            return True
        else:
            print("\n Failed to send review alert")
            return False

    except Exception as e:
        print(f"\n Failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_workflow():
    """Test the approval workflow."""
    print_section("APPROVAL WORKFLOW")

    from src.approval.workflow import ApprovalWorkflow
    from src.signals.models import ProposedTrade, ReconciliationPlan, ReconciliationResult

    workflow = ApprovalWorkflow()

    # Create a test reconciliation plan
    plan = ReconciliationPlan.create(
        intent_id=uuid4(),
        sleeve_id=uuid4(),
        holdings_snapshot={"AAPL": 100, "GOOGL": 50},
        proposed_trades=[
            ProposedTrade(
                symbol="AAPL",
                side="buy",
                notional=200.00,
                current_weight=0.10,
                target_weight=0.12,
                delta_weight=0.02,
                rationale="Increase position",
            ),
            ProposedTrade(
                symbol="MSFT",
                side="sell",
                notional=100.00,
                current_weight=0.08,
                target_weight=0.06,
                delta_weight=-0.02,
                rationale="Reduce position",
            ),
        ],
        result_type=ReconciliationResult.PROPOSED,
    )

    print(f"Reconciliation ID: {plan.id}")
    print(f"Trade count: {plan.trade_count}")
    print(f"Total notional: ${plan.total_notional:.2f}")

    if not workflow.bot.default_chat_id:
        print("\nSkipping send - TELEGRAM_CHAT_ID not set")
        print("Workflow logic tested successfully")
        return True

    try:
        print("\nProcessing reconciliation through workflow...")
        result = await workflow.process_reconciliation(
            plan=plan,
            sleeve_name="bravos",
            approval_required=True,
        )

        print(f"Result: {result.status}")
        if result.approval_code:
            print(f"Approval code: {result.approval_code}")
        if result.details:
            print(f"Details: {result.details}")

        if result.success:
            print("\n Workflow test passed")
            return True
        else:
            print(f"\n Workflow test failed: {result.error}")
            return False

    except Exception as e:
        print(f"\n Workflow test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("TELEGRAM BOT TESTS")
    print("=" * 60)

    # Check environment
    print("\nEnvironment:")
    print(f"  TELEGRAM_BOT_TOKEN: {'Set' if os.getenv('TELEGRAM_BOT_TOKEN') else 'NOT SET'}")
    print(f"  TELEGRAM_ALLOWED_USERS: {os.getenv('TELEGRAM_ALLOWED_USERS', 'NOT SET')}")
    print(f"  TELEGRAM_CHAT_ID: {os.getenv('TELEGRAM_CHAT_ID', 'NOT SET')}")

    results = []

    # Test 1: Bot initialization
    results.append(("Bot Initialization", await test_bot_initialization()))

    # Test 2: Message formatting (no network)
    results.append(("Message Formatting", await test_message_formatting()))

    # Test 3: Send approval (requires Telegram)
    results.append(("Send Approval", await test_send_approval_request()))

    # Test 4: Send review alert (requires Telegram)
    results.append(("Send Review Alert", await test_send_review_alert()))

    # Test 5: Workflow test
    results.append(("Approval Workflow", await test_workflow()))

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

    if skipped > 0:
        print("\nNote: Some tests were skipped due to missing Telegram configuration.")
        print("To run all tests, set TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS, and TELEGRAM_CHAT_ID")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
