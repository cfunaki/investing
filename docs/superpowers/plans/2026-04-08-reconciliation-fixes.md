# Reconciliation Pipeline Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five bugs causing incorrect trade proposals, duplicate approvals, and poll failures.

**Architecture:** Targeted fixes to the existing pipeline — bootstrap the virtual ledger from RH holdings, fix sell calculations to use shares, mark emails as "processing" immediately, remove auto-retrigger spam, and fix stale Gmail connections.

**Tech Stack:** Python, SQLAlchemy async, FastAPI, robin_stocks, Google Gmail API

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/db/migrations/004_signal_retry_count.sql` | Create | Add retry_count column to signals table |
| `src/reconciliation/delta_reconciler.py` | Modify | Fix sell calculations to use shares from ledger |
| `src/reconciliation/bootstrap.py` | Create | Bootstrap sleeve_positions from RH + Bravos |
| `src/signals/bravos_processor.py` | Modify | Mark email as "processing" before reconciliation |
| `src/signals/bravos_detector.py` | Modify | Add mark_as_processing() method |
| `src/db/repositories/signal_repository.py` | Modify | Add retry_count support |
| `src/approval/workflow.py` | Modify | Remove auto-retrigger logic |
| `src/approval/telegram.py` | Modify | Add /sync command |
| `src/signals/email_monitor.py` | Modify | Clear cached Gmail service per call |
| `src/api/main.py` | Modify | Run ledger bootstrap on startup |

---

### Task 1: Database Migration — Add retry_count to signals

**Files:**
- Create: `src/db/migrations/004_signal_retry_count.sql`
- Modify: `src/db/models.py:171-175`

- [ ] **Step 1: Create the migration file**

```sql
-- Signal retry tracking for transient email processing failures
-- Run automatically on startup via migrator

ALTER TABLE signals ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
```

Save to `src/db/migrations/004_signal_retry_count.sql`.

- [ ] **Step 2: Add retry_count to Signal model**

In `src/db/models.py`, after the `error_message` field (line 175), add:

```python
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
```

Ensure `Integer` is imported from sqlalchemy (check existing imports at top of file).

- [ ] **Step 3: Commit**

```bash
git add src/db/migrations/004_signal_retry_count.sql src/db/models.py
git commit -m "feat: add retry_count column to signals for email processing retries"
```

---

### Task 2: Fix Sell Calculations — Use Shares, Not Notional

**Files:**
- Modify: `src/reconciliation/delta_reconciler.py:44-52` (DeltaTrade dataclass)
- Modify: `src/reconciliation/delta_reconciler.py:86-155` (reconcile + _generate_trades)

- [ ] **Step 1: Add quantity field to DeltaTrade**

In `src/reconciliation/delta_reconciler.py`, update the `DeltaTrade` dataclass (line 44):

```python
@dataclass
class DeltaTrade:
    """A single trade to execute based on weight change."""

    symbol: str
    side: str  # 'buy' or 'sell'
    notional: Decimal  # Dollar amount (for buys)
    weight_delta: Decimal  # Weight units changed
    target_weight: Decimal  # Final weight after this trade
    rationale: str
    quantity: Decimal | None = None  # Share quantity (for sells)
```

- [ ] **Step 2: Pass current_positions to _generate_trades**

In the `reconcile()` method (~line 126), change:

```python
            # Generate trades for changes
            trades = self._generate_trades(weight_changes)
```

to:

```python
            # Generate trades for changes
            trades = self._generate_trades(weight_changes, current_positions)
```

- [ ] **Step 3: Update _generate_trades to use shares for sells**

Replace the `_generate_trades` method (lines 198-254) with:

```python
    def _generate_trades(
        self,
        weight_changes: list[WeightChange],
        current_positions: dict[str, "SleevePosition"],
    ) -> list[DeltaTrade]:
        """Generate trades from weight changes.

        Buys use notional (weight_delta * unit_size).
        Sells use actual share quantities from the ledger.
        """
        trades = []

        for change in weight_changes:
            if change.action == "unchanged":
                continue

            delta = abs(change.weight_delta)
            notional = delta * self.unit_size

            if change.action == "enter":
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="buy",
                        notional=notional,
                        weight_delta=change.new_weight,
                        target_weight=change.new_weight,
                        rationale=f"New position: weight {change.new_weight}",
                    )
                )
            elif change.action == "exit":
                position = current_positions.get(change.symbol)
                shares = position.shares if position else Decimal(0)
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="sell",
                        notional=Decimal(0),
                        weight_delta=-change.old_weight,
                        target_weight=Decimal(0),
                        rationale=f"Exit position: sell all {shares} shares",
                        quantity=shares,
                    )
                )
            elif change.action == "increase":
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="buy",
                        notional=notional,
                        weight_delta=change.weight_delta,
                        target_weight=change.new_weight,
                        rationale=f"Increase weight: {change.old_weight} → {change.new_weight}",
                    )
                )
            elif change.action == "decrease":
                position = current_positions.get(change.symbol)
                if position and position.shares > 0 and change.old_weight > 0:
                    sell_fraction = delta / change.old_weight
                    shares_to_sell = (position.shares * sell_fraction).quantize(Decimal("0.000001"))
                else:
                    shares_to_sell = Decimal(0)
                trades.append(
                    DeltaTrade(
                        symbol=change.symbol,
                        side="sell",
                        notional=Decimal(0),
                        weight_delta=change.weight_delta,
                        target_weight=change.new_weight,
                        rationale=f"Decrease weight: {change.old_weight} → {change.new_weight}, sell {shares_to_sell} shares",
                        quantity=shares_to_sell,
                    )
                )

        return trades
```

- [ ] **Step 4: Update trade-to-dict conversion downstream**

In `src/signals/bravos_processor.py`, find where `DeltaTrade` objects are converted to dicts for the approval flow (search for `proposed_trades`). Ensure the `quantity` field is included. The conversion likely happens around line 310-340. Add `"quantity"` to the trade dict:

```python
# In the trade dict construction, add:
"quantity": float(trade.quantity) if trade.quantity else None,
```

- [ ] **Step 5: Commit**

```bash
git add src/reconciliation/delta_reconciler.py src/signals/bravos_processor.py
git commit -m "fix: sells use actual share quantities from ledger, not notional"
```

---

### Task 3: Ledger Bootstrap — Cross-Reference RH Holdings with Bravos

**Files:**
- Create: `src/reconciliation/bootstrap.py`

- [ ] **Step 1: Create the bootstrap module**

```python
"""
Bootstrap the sleeve virtual ledger from actual Robinhood holdings.

Cross-references Bravos target symbols with RH positions to seed
sleeve_positions with actual share quantities.
"""

import json
import logging
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from src.brokers.robinhood import get_robinhood_adapter
from src.db.repositories.sleeve_position_repository import sleeve_position_repository
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context
from src.signals.bravos_processor import BRAVOS_TRADES_PATH, parse_bravos_weights

logger = logging.getLogger(__name__)


async def bootstrap_ledger(force: bool = False) -> dict:
    """
    Bootstrap sleeve_positions from RH holdings × Bravos targets.

    Args:
        force: If False, only runs when ledger is empty. If True, always runs.

    Returns:
        Dict with bootstrap results (seeded count, skipped, errors).
    """
    # Get bravos sleeve
    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, "bravos")
        if not sleeve:
            logger.warning("bootstrap_no_bravos_sleeve")
            return {"error": "No bravos sleeve found in database"}

        sleeve_id = sleeve.id

        # Check if ledger already has positions (skip unless forced)
        if not force:
            positions = await sleeve_position_repository.get_position_map(db, sleeve_id)
            if positions:
                logger.info("bootstrap_skipped_ledger_has_positions", count=len(positions))
                return {"skipped": True, "existing_positions": len(positions)}

    # Get Bravos target symbols
    bravos_symbols = set()
    bravos_weights = {}
    if BRAVOS_TRADES_PATH.exists():
        with open(BRAVOS_TRADES_PATH) as f:
            bravos_data = json.load(f)
        bravos_weights = parse_bravos_weights(bravos_data)
        bravos_symbols = set(bravos_weights.keys())
    else:
        logger.warning("bootstrap_no_bravos_data", path=str(BRAVOS_TRADES_PATH))
        return {"error": "No bravos_trades.json found. Run a scrape first."}

    if not bravos_symbols:
        return {"error": "No Bravos symbols found"}

    logger.info("bootstrap_bravos_symbols", symbols=sorted(bravos_symbols))

    # Get RH holdings
    broker = get_robinhood_adapter()
    if not await broker.is_connected():
        connected = await broker.connect()
        if not connected:
            logger.warning("bootstrap_rh_not_connected")
            return {"error": "Cannot connect to Robinhood. Use /login first."}

    rh_positions = await broker.get_positions()
    rh_by_symbol = {p.symbol: p for p in rh_positions}

    logger.info("bootstrap_rh_positions", count=len(rh_positions))

    # Cross-reference and seed
    seeded = []
    not_in_rh = []

    async with get_db_context() as db:
        for symbol in bravos_symbols:
            rh_pos = rh_by_symbol.get(symbol)
            if rh_pos and rh_pos.quantity > 0:
                weight = bravos_weights.get(symbol, Decimal(0))
                await sleeve_position_repository.upsert_position(
                    db=db,
                    sleeve_id=sleeve_id,
                    symbol=symbol,
                    shares=Decimal(str(rh_pos.quantity)),
                    weight=weight,
                    cost_basis=Decimal(str(rh_pos.quantity * rh_pos.average_cost)),
                )
                seeded.append(symbol)
                logger.info(
                    "bootstrap_seeded_position",
                    symbol=symbol,
                    shares=rh_pos.quantity,
                    weight=float(weight),
                )
            else:
                not_in_rh.append(symbol)

    result = {
        "seeded": len(seeded),
        "seeded_symbols": sorted(seeded),
        "not_in_rh": sorted(not_in_rh),
    }
    logger.info("bootstrap_complete", **result)
    return result
```

- [ ] **Step 2: Commit**

```bash
git add src/reconciliation/bootstrap.py
git commit -m "feat: add ledger bootstrap from RH holdings × Bravos targets"
```

---

### Task 4: Wire Bootstrap to Startup and /sync Command

**Files:**
- Modify: `src/api/main.py:86-95` (lifespan startup)
- Modify: `src/approval/telegram.py` (add /sync command)

- [ ] **Step 1: Add bootstrap to startup**

In `src/api/main.py`, after the migration runner call (~line 93), add:

```python
        # Bootstrap ledger if empty (first-time setup)
        try:
            from src.reconciliation.bootstrap import bootstrap_ledger
            result = await bootstrap_ledger(force=False)
            logger.info(f"Ledger bootstrap: {result}")
        except Exception as e:
            logger.warning(f"Ledger bootstrap failed (non-fatal): {e}")
```

- [ ] **Step 2: Add /sync command to Telegram bot**

In `src/approval/telegram.py`, find where command handlers are registered (search for `add_handler.*CommandHandler`). Add:

```python
        app.add_handler(CommandHandler("sync", self._cmd_sync))
```

Then add the handler method (place it near `_cmd_cancel_queued`):

```python
    async def _cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /sync command - re-bootstrap sleeve ledger from RH holdings."""
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("Unauthorized")
            return

        await update.message.reply_text("Syncing ledger from Robinhood holdings...")

        try:
            from src.reconciliation.bootstrap import bootstrap_ledger
            result = await bootstrap_ledger(force=True)

            if "error" in result:
                await update.message.reply_text(
                    f"*Sync Failed*\n{result['error']}",
                    parse_mode="Markdown",
                )
                return

            seeded = result.get("seeded", 0)
            symbols = result.get("seeded_symbols", [])
            not_in_rh = result.get("not_in_rh", [])

            msg = f"*Ledger Synced*\n\n"
            msg += f"Seeded: {seeded} positions\n"
            if symbols:
                msg += f"Symbols: {', '.join(symbols)}\n"
            if not_in_rh:
                msg += f"Not in RH: {', '.join(not_in_rh)}"

            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as e:
            logger.exception("sync_command_failed", error=str(e))
            await update.message.reply_text(f"Sync error: {str(e)}")
```

Also add `/sync` to the help text string where other commands are listed.

- [ ] **Step 3: Commit**

```bash
git add src/api/main.py src/approval/telegram.py
git commit -m "feat: wire ledger bootstrap to startup and /sync Telegram command"
```

---

### Task 5: Email Dedup — Mark as Processing Immediately

**Files:**
- Modify: `src/signals/bravos_detector.py:107-162` (add mark_as_processing method)
- Modify: `src/db/repositories/signal_repository.py:92-114` (add increment_retry)
- Modify: `src/signals/bravos_processor.py:130-159` (restructure processing flow)

- [ ] **Step 1: Add mark_as_processing to bravos_detector**

In `src/signals/bravos_detector.py`, add this method before `mark_as_processed` (before line 107):

```python
    async def mark_as_processing(
        self,
        message_id: str,
        subject: str | None = None,
    ) -> UUID | None:
        """
        Mark an email as 'processing' immediately on detection.
        Returns the signal ID for later status updates.
        """
        sleeve_id = await self._get_sleeve_id()
        if not sleeve_id:
            logger.error("cannot_mark_processing_no_sleeve_id")
            return None

        try:
            async with get_db_context() as db:
                existing = await signal_repository.get_by_source_event_id(
                    db, sleeve_id, message_id
                )
                if existing:
                    # Already exists — update to processing if it's a retry
                    if existing.status == "retry":
                        await signal_repository.update_status(
                            db, existing.id, status="processing"
                        )
                    return existing.id
                else:
                    signal = await signal_repository.create(
                        db=db,
                        sleeve_id=sleeve_id,
                        source_event_id=message_id,
                        event_type="email_detected",
                        detected_at=datetime.now(timezone.utc),
                        raw_payload={"subject": subject},
                        status="processing",
                    )
                    logger.info("marked_email_as_processing", message_id=message_id)
                    return signal.id

        except Exception as e:
            logger.exception("failed_to_mark_as_processing", error=str(e))
            return None
```

- [ ] **Step 2: Add increment_retry_count to signal_repository**

In `src/db/repositories/signal_repository.py`, add after the `update_status` method (after line 114):

```python
    async def increment_retry(
        self,
        db: AsyncSession,
        signal_id: UUID,
    ) -> int:
        """Increment retry count and return new count."""
        stmt = (
            update(Signal)
            .where(Signal.id == signal_id)
            .values(
                retry_count=Signal.retry_count + 1,
                status="retry",
            )
            .returning(Signal.retry_count)
        )
        result = await db.execute(stmt)
        return result.scalar_one()
```

- [ ] **Step 3: Restructure bravos_processor to mark early and handle retries**

Replace the processing flow in `src/signals/bravos_processor.py` (lines 130-159) with:

```python
        # Mark as processing immediately (prevents duplicate detection)
        signal_id = None
        if email and not dry_run:
            signal_id = await self.detector.mark_as_processing(
                message_id=email.message_id,
                subject=email.subject,
            )

        # Process the email/trigger reconciliation
        try:
            result = await self._process_reconciliation(
                email=email,
                dry_run=dry_run,
                skip_scrape=skip_scrape,
            )

            # Mark as fully processed
            if result.success and not dry_run and signal_id:
                await self.detector.mark_as_processed(
                    message_id=email.message_id,
                    subject=email.subject,
                    details={
                        "trade_count": result.trade_count,
                        "total_buy": result.total_buy,
                        "total_sell": result.total_sell,
                    },
                )

            return result

        except Exception as e:
            log.exception("processing_failed", error=str(e))

            # Handle retry logic for transient errors
            if signal_id and email and not dry_run:
                is_transient = isinstance(e, (BrokenPipeError, ConnectionError, TimeoutError, OSError))
                if is_transient:
                    try:
                        async with get_db_context() as db:
                            retry_count = await signal_repository.increment_retry(db, signal_id)

                        if retry_count >= 3:
                            # Max retries exceeded — mark as failed, notify
                            async with get_db_context() as db:
                                await signal_repository.update_status(
                                    db, signal_id, status="failed",
                                    error_message=f"Max retries exceeded: {str(e)}",
                                )
                            try:
                                from src.approval.telegram import get_telegram_bot
                                bot = get_telegram_bot()
                                await bot.send_notification(
                                    f"*Email Processing Failed*\n\n"
                                    f"Message: {email.subject}\n"
                                    f"Error: {str(e)}\n"
                                    f"Retries: {retry_count}/3\n\n"
                                    f"Use `force=true` to reprocess.",
                                )
                            except Exception:
                                pass
                        else:
                            log.info("email_queued_for_retry", retry_count=retry_count)
                    except Exception as retry_err:
                        log.warning("retry_tracking_failed", error=str(retry_err))
                else:
                    # Persistent error — mark as failed immediately
                    try:
                        async with get_db_context() as db:
                            await signal_repository.update_status(
                                db, signal_id, status="failed",
                                error_message=str(e),
                            )
                    except Exception:
                        pass

            return BravosProcessingResult(
                success=False,
                new_email=True if email else False,
                message_id=email.message_id if email else None,
                error=str(e),
            )
```

Add the needed imports at the top of `bravos_processor.py`:

```python
from src.db.repositories.signal_repository import signal_repository
from src.db.session import get_db_context
```

- [ ] **Step 4: Commit**

```bash
git add src/signals/bravos_detector.py src/signals/bravos_processor.py src/db/repositories/signal_repository.py
git commit -m "fix: mark emails as processing immediately to prevent duplicate approvals"
```

---

### Task 6: Remove Auto-Retrigger

**Files:**
- Modify: `src/approval/workflow.py:674-831`

- [ ] **Step 1: Simplify expire_pending_approvals**

Replace `expire_pending_approvals` method (lines 674-751) with:

```python
    async def expire_pending_approvals(self) -> int:
        """
        Expire any pending approvals that have passed their expiration time.

        Returns:
            Number of approvals expired
        """
        expired_count = 0
        now = datetime.now(timezone.utc)

        # Expire in-memory approvals
        for code, record in list(self._approvals.items()):
            if record.status == ApprovalStatus.PENDING and record.is_expired():
                record.status = ApprovalStatus.EXPIRED
                expired_count += 1

                logger.info(
                    "approval_expired",
                    approval_code=code,
                    approval_id=str(record.id),
                )

                # Update Telegram message
                await self.bot.update_approval_message(
                    code,
                    f"*Approval Expired*\n"
                    f"Code: `{code}`\n"
                    f"Expired at: {now.strftime('%Y-%m-%d %H:%M UTC')}",
                )

        # Expire database approvals
        try:
            async with get_db_context() as db:
                db_expired = await approval_repository.get_expired(db)
                for db_approval in db_expired:
                    await approval_repository.mark_expired(db, db_approval.id)
                    if db_approval.approval_code not in self._approvals:
                        expired_count += 1
                        logger.info(
                            "db_approval_expired",
                            approval_code=db_approval.approval_code,
                            approval_id=str(db_approval.id),
                        )
        except Exception as e:
            logger.warning("failed_to_expire_db_approvals", error=str(e))

        return expired_count
```

- [ ] **Step 2: Delete the _retrigger_approval method**

Delete the entire `_retrigger_approval` method (lines 753-831). Remove any imports that were only used by this method (check for `Reconciliation` model import if it's only used there).

- [ ] **Step 3: Commit**

```bash
git add src/approval/workflow.py
git commit -m "fix: remove auto-retrigger on approval expiry"
```

---

### Task 7: Fix Gmail Broken Pipe

**Files:**
- Modify: `src/signals/email_monitor.py`

- [ ] **Step 1: Clear cached Gmail service before each poll**

In `src/signals/email_monitor.py`, find the `check_for_emails` method. At the very start of the method body, add:

```python
        # Force fresh connection to avoid stale socket errors
        self._service = None
```

This ensures `_get_service()` rebuilds the HTTP transport on every call, preventing `[Errno 32] Broken pipe` from stale connections between 30-minute polling intervals.

- [ ] **Step 2: Commit**

```bash
git add src/signals/email_monitor.py
git commit -m "fix: rebuild Gmail API connection each poll to prevent broken pipe errors"
```

---

## Self-Review

**Spec coverage:**
- Fix 1 (ledger bootstrap): Task 3 + Task 4 ✓
- Fix 2 (sell calculations): Task 2 ✓
- Fix 3 (email dedup): Task 1 + Task 5 ✓
- Fix 4 (remove auto-retrigger): Task 6 ✓
- Fix 5 (Gmail broken pipe): Task 7 ✓

**Placeholder scan:** All code blocks contain complete implementations. No TBD/TODO.

**Type consistency:** `DeltaTrade.quantity` is `Decimal | None` throughout. `signal_id` is `UUID | None`. `bootstrap_ledger` returns `dict` consistently.
