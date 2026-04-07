# System Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate data loss on container restarts, fix Gmail token persistence, add off-hours trade queuing, and improve job error handling.

**Architecture:** Move all ephemeral filesystem state into the existing Supabase PostgreSQL database. Add a `key_value_state` table for simple blobs, `entry_prices` for Bravos prices, and `queued_executions` for off-hours trades. Wire up existing email dedup infrastructure. Persist Gmail token refreshes to Secret Manager.

**Tech Stack:** SQLAlchemy (async), PostgreSQL/Supabase, google-cloud-secret-manager, FastAPI

**Spec:** `docs/superpowers/specs/2026-04-06-system-reliability-design.md`

---

### Task 1: Database Migration — New Tables

**Files:**
- Create: `src/db/migrations/003_system_reliability.sql`
- Modify: `src/db/models.py` (add 3 new SQLAlchemy models at bottom, before existing singletons)

- [ ] **Step 1: Write the SQL migration**

Create `src/db/migrations/003_system_reliability.sql`:

```sql
-- System Reliability: state persistence, entry prices, queued executions
-- Run via Supabase SQL Editor

CREATE TABLE IF NOT EXISTS key_value_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entry_prices (
    symbol TEXT PRIMARY KEY,
    price NUMERIC NOT NULL,
    source TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS queued_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_id UUID NOT NULL REFERENCES approvals(id),
    trades JSONB NOT NULL,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    execute_after TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    executed_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_queued_executions_status
    ON queued_executions(status, execute_after);

-- Seed entry_prices from known values
INSERT INTO entry_prices (symbol, price, source) VALUES
    ('ALUM', 3.85, 'cache_import'),
    ('EME', 687.73, 'cache_import'),
    ('D', 59.5, 'cache_import'),
    ('FHI', 54.42, 'cache_import'),
    ('DBC', 24.76, 'cache_import'),
    ('HSY', 211.75, 'cache_import'),
    ('NTR', 66.5, 'cache_import'),
    ('AA', 56.7, 'cache_import'),
    ('EXC', 49.82, 'cache_import'),
    ('ANDE', 58.5, 'cache_import'),
    ('CENX', 55.5, 'cache_import'),
    ('NEE', 92.2, 'cache_import'),
    ('LIN', 487, 'cache_import')
ON CONFLICT (symbol) DO NOTHING;
```

- [ ] **Step 2: Run the migration in Supabase**

Open Supabase SQL Editor and paste the migration. Verify tables created:

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_name IN ('key_value_state', 'entry_prices', 'queued_executions');
```

Expected: 3 rows returned.

- [ ] **Step 3: Add SQLAlchemy models**

Add to `src/db/models.py` after the `IdempotencyKey` model (around line 454), before the file ends:

```python
class KeyValueState(Base):
    """Simple key-value store for runtime state."""

    __tablename__ = "key_value_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EntryPrice(Base):
    """Bravos entry prices — write-once per symbol."""

    __tablename__ = "entry_prices"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    price: Mapped[float] = mapped_column(Numeric, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QueuedExecution(Base):
    """Approved trades waiting for market open."""

    __tablename__ = "queued_executions"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    approval_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("approvals.id"), nullable=False
    )
    trades: Mapped[dict] = mapped_column(JSONB, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    execute_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(String, nullable=True)
```

Ensure `Numeric` is imported at the top of the file. Check existing imports — `JSONB`, `String`, `DateTime`, `ForeignKey`, `PgUUID`, `func`, `Mapped`, `mapped_column` should already be imported. Add `Numeric` to the sqlalchemy import line if missing.

- [ ] **Step 4: Commit**

```bash
git add src/db/migrations/003_system_reliability.sql src/db/models.py
git commit -m "feat: add key_value_state, entry_prices, queued_executions tables"
```

---

### Task 2: State Persistence Repository

**Files:**
- Create: `src/db/repositories/state_repository.py`
- Modify: `src/db/repositories/__init__.py` (add export)

- [ ] **Step 1: Create state repository**

Create `src/db/repositories/state_repository.py`:

```python
"""
Repository for state persistence operations.

Covers key_value_state, entry_prices, and queued_executions tables.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import EntryPrice, KeyValueState, QueuedExecution


class StateRepository:
    """Data access layer for runtime state."""

    # ── key_value_state ──────────────────────────────────────────────

    async def get_state(self, db: AsyncSession, key: str) -> dict[str, Any] | None:
        """Get a state value by key."""
        stmt = select(KeyValueState.value).where(KeyValueState.key == key)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        return row

    async def set_state(self, db: AsyncSession, key: str, value: dict[str, Any]) -> None:
        """Set a state value (upsert)."""
        stmt = pg_insert(KeyValueState).values(
            key=key,
            value=value,
            updated_at=datetime.now(timezone.utc),
        ).on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": value,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        await db.execute(stmt)

    # ── entry_prices ─────────────────────────────────────────────────

    async def get_entry_price(self, db: AsyncSession, symbol: str) -> float | None:
        """Get entry price for a symbol."""
        stmt = select(EntryPrice.price).where(EntryPrice.symbol == symbol.upper())
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_entry_prices(self, db: AsyncSession) -> dict[str, float]:
        """Get all entry prices as a dict."""
        stmt = select(EntryPrice.symbol, EntryPrice.price)
        result = await db.execute(stmt)
        return {row[0]: float(row[1]) for row in result.all()}

    async def upsert_entry_price(
        self, db: AsyncSession, symbol: str, price: float, source: str
    ) -> bool:
        """Insert entry price only if not already present. Returns True if inserted."""
        stmt = pg_insert(EntryPrice).values(
            symbol=symbol.upper(),
            price=price,
            source=source,
        ).on_conflict_do_nothing(index_elements=["symbol"])
        result = await db.execute(stmt)
        return result.rowcount > 0

    # ── queued_executions ────────────────────────────────────────────

    async def queue_execution(
        self,
        db: AsyncSession,
        approval_id: UUID,
        trades: list[dict[str, Any]],
        execute_after: datetime,
    ) -> QueuedExecution:
        """Queue trades for later execution."""
        record = QueuedExecution(
            approval_id=approval_id,
            trades=trades,
            execute_after=execute_after,
            status="pending",
        )
        db.add(record)
        await db.flush()
        return record

    async def get_pending_executions(
        self, db: AsyncSession, before: datetime | None = None
    ) -> list[QueuedExecution]:
        """Get pending queued executions ready to run."""
        stmt = select(QueuedExecution).where(
            QueuedExecution.status == "pending",
        )
        if before:
            stmt = stmt.where(QueuedExecution.execute_after <= before)
        stmt = stmt.order_by(QueuedExecution.queued_at)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def update_queued_status(
        self,
        db: AsyncSession,
        queued_id: UUID,
        status: str,
        error: str | None = None,
    ) -> None:
        """Update queued execution status."""
        values: dict[str, Any] = {"status": status}
        if status in ("completed", "failed"):
            values["executed_at"] = datetime.now(timezone.utc)
        if error:
            values["error"] = error
        stmt = update(QueuedExecution).where(
            QueuedExecution.id == queued_id,
        ).values(**values)
        await db.execute(stmt)

    async def cancel_pending_executions(self, db: AsyncSession) -> int:
        """Cancel all pending queued executions. Returns count cancelled."""
        stmt = update(QueuedExecution).where(
            QueuedExecution.status == "pending",
        ).values(status="cancelled")
        result = await db.execute(stmt)
        return result.rowcount


state_repository = StateRepository()
```

- [ ] **Step 2: Export from `__init__.py`**

Add to `src/db/repositories/__init__.py`:

```python
from src.db.repositories.state_repository import state_repository
```

- [ ] **Step 3: Commit**

```bash
git add src/db/repositories/state_repository.py src/db/repositories/__init__.py
git commit -m "feat: add state persistence repository for kv store, entry prices, queued executions"
```

---

### Task 3: Entry Prices — Migrate from JSON to Database

**Files:**
- Modify: `src/signals/bravos_processor.py` (replace JSON file reads with DB queries)
- Modify: `src/approval/telegram.py` (price context uses DB)
- Modify: `Dockerfile` (remove COPY of bravos_entry_prices.json)

- [ ] **Step 1: Update bravos_processor.py to use database for entry prices**

In `src/signals/bravos_processor.py`, find the entry price loading section (around lines 333-357). Replace the entire block:

Old code (lines 333-357):
```python
        # Step 6: Load Bravos entry prices (best-effort)
        # Check both the scraped data and the persistent entry price cache
        bravos_entry_prices: dict[str, float] = {}

        # Source 1: Local scraper format has entryPrice in trades dict
        trades_data = bravos_data.get("trades", {})
        for sym, info in trades_data.items():
            entry = info.get("entryPrice")
            if entry and entry > 0:
                bravos_entry_prices[sym.upper()] = float(entry)

        # Source 2: Persistent entry price cache (survives browser worker overwrites)
        entry_cache_path = Path("data/state/bravos_entry_prices.json")
        if entry_cache_path.exists():
            try:
                with open(entry_cache_path) as f:
                    cached = json.load(f)
                for sym, price in cached.items():
                    if sym.upper() not in bravos_entry_prices and price and price > 0:
                        bravos_entry_prices[sym.upper()] = float(price)
            except Exception:
                pass

        if bravos_entry_prices:
            log.info("bravos_entry_prices_loaded", count=len(bravos_entry_prices))
```

New code:
```python
        # Step 6: Load and persist Bravos entry prices
        bravos_entry_prices: dict[str, float] = {}

        try:
            from src.db.repositories.state_repository import state_repository
            from src.db.session import get_db_context

            # Source 1: Scraper data — persist new entry prices to DB
            trades_data = bravos_data.get("trades", {})
            async with get_db_context() as db:
                for sym, info in trades_data.items():
                    entry = info.get("entryPrice")
                    if entry and entry > 0:
                        await state_repository.upsert_entry_price(
                            db, sym.upper(), float(entry), "bravos_scrape"
                        )

                # Source 2: Load all entry prices from DB
                bravos_entry_prices = await state_repository.get_all_entry_prices(db)

            if bravos_entry_prices:
                log.info("bravos_entry_prices_loaded", count=len(bravos_entry_prices))
        except Exception as e:
            log.warning("bravos_entry_prices_load_failed", error=str(e))
```

Also remove the `from pathlib import Path` import if it's no longer used elsewhere in the file. Check before removing.

- [ ] **Step 2: Remove baked-in JSON from Dockerfile**

In `Dockerfile`, remove line 43:

```dockerfile
COPY data/state/bravos_entry_prices.json ./data/state/
```

- [ ] **Step 3: Commit**

```bash
git add src/signals/bravos_processor.py Dockerfile
git commit -m "feat: migrate entry prices from JSON file to database"
```

---

### Task 4: Email Deduplication — Wire Up Existing Infrastructure

**Files:**
- Modify: `src/api/main.py` (replace empty `processed_ids` with DB query)

- [ ] **Step 1: Update poll-email job to use database dedup**

In `src/api/main.py`, find the poll-email job (around line 316). Replace the TODO block:

Old code:
```python
        # TODO: Load processed message IDs from database
        # For now, we process all found emails
        # In production, query signals table for existing source_event_ids
        processed_ids: set[str] = set()
```

New code:
```python
        # Load already-processed message IDs from database
        from src.signals.bravos_detector import get_bravos_detector
        try:
            detector = get_bravos_detector()
            processed_ids = await detector.get_processed_message_ids()
            log.info("loaded_processed_ids", count=len(processed_ids))
        except Exception as e:
            log.warning("failed_to_load_processed_ids", error=str(e))
            processed_ids = set()
```

Check that `get_bravos_detector` exists. If not, look for however the detector is instantiated (may be a module-level singleton like the repositories).

- [ ] **Step 2: Verify the import works**

Run: `cd /Users/chris.funaki/Documents/GitHub/investing && python -c "from src.signals.bravos_detector import get_bravos_detector; print('OK')"`

If this fails, check the actual function/class name in `src/signals/bravos_detector.py` and adjust the import.

- [ ] **Step 3: Commit**

```bash
git add src/api/main.py
git commit -m "feat: wire email dedup to database via bravos_detector"
```

---

### Task 5: Gmail Token Refresh — Persist to Secret Manager

**Files:**
- Modify: `src/signals/email_monitor.py` (add Secret Manager writeback after refresh)
- Modify: `requirements.txt` (add google-cloud-secret-manager)

- [ ] **Step 1: Add dependency**

Add to `requirements.txt`:

```
google-cloud-secret-manager>=2.18.0
```

- [ ] **Step 2: Update token refresh in email_monitor.py**

In `src/signals/email_monitor.py`, find the refresh block (around lines 123-139). Replace:

Old code:
```python
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("refreshed_gmail_credentials")
            
            # If we loaded from JSON, save refreshed token back to file for debugging
            if self.token_json and not token_path.exists():
                try:
                    token_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(token_path, "w") as token_file:
                        token_file.write(creds.to_json())
                    logger.info("saved_refreshed_token_to_file", path=str(token_path))
                except Exception:
                    pass  # Not critical, Cloud Run may not have writable filesystem
        except Exception as e:
            logger.warning("failed_to_refresh_credentials", error=str(e))
            creds = None
```

New code:
```python
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("refreshed_gmail_credentials")
            
            # Persist refreshed token to Secret Manager
            self._persist_refreshed_token(creds)
        except Exception as e:
            logger.warning("failed_to_refresh_credentials", error=str(e))
            creds = None
```

Then add a new method to the `EmailMonitor` class:

```python
def _persist_refreshed_token(self, creds: Credentials) -> None:
    """Write refreshed Gmail token to Secret Manager and database."""
    token_json = creds.to_json()

    # 1. Try Secret Manager
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "investing-automation-490206")
        secret_path = f"projects/{project_id}/secrets/GMAIL_TOKEN_JSON"
        client.add_secret_version(
            request={
                "parent": secret_path,
                "payload": {"data": token_json.encode("utf-8")},
            }
        )
        logger.info("gmail_token_persisted_to_secret_manager")
    except Exception as e:
        logger.warning("gmail_token_secret_manager_write_failed", error=str(e))

    # 2. Backup to database key_value_state
    try:
        import asyncio
        from src.db.repositories.state_repository import state_repository
        from src.db.session import get_db_context

        async def _save():
            async with get_db_context() as db:
                await state_repository.set_state(db, "gmail_token", {
                    "token_json": token_json,
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                })

        # This runs in a sync context (Gmail API is sync), so use run_coroutine_threadsafe
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(_save(), loop)
            future.result(timeout=10)
        except RuntimeError:
            # No running loop — run directly
            asyncio.run(_save())
        logger.info("gmail_token_persisted_to_database")
    except Exception as e:
        logger.warning("gmail_token_database_write_failed", error=str(e))
```

Add `import os` to the top of the file if not already imported. Also ensure `from datetime import datetime, timezone` is present.

- [ ] **Step 3: Add database fallback to credential loading**

In the `_get_credentials()` method, after the Secret Manager and file loading attempts (before the refresh check), add a database fallback:

```python
    # Priority 3: Check database backup (key_value_state)
    if not creds:
        try:
            import asyncio
            from src.db.repositories.state_repository import state_repository
            from src.db.session import get_db_context

            async def _load():
                async with get_db_context() as db:
                    return await state_repository.get_state(db, "gmail_token")

            try:
                loop = asyncio.get_running_loop()
                future = asyncio.run_coroutine_threadsafe(_load(), loop)
                state = future.result(timeout=10)
            except RuntimeError:
                state = asyncio.run(_load())

            if state and state.get("token_json"):
                token_data = json.loads(state["token_json"])
                creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                logger.info("loaded_gmail_token_from_database")
        except Exception as e:
            logger.warning("failed_to_load_token_from_database", error=str(e))
```

- [ ] **Step 4: Commit**

```bash
git add requirements.txt src/signals/email_monitor.py
git commit -m "feat: persist Gmail token refresh to Secret Manager with DB fallback"
```

---

### Task 6: Off-Hours Order Queuing — Executor Changes

**Files:**
- Modify: `src/execution/executor.py` (queue instead of block when market closed)
- Modify: `src/execution/safety.py` (expose market_closed check result separately)

- [ ] **Step 1: Add helper to calculate next market open**

Add to `src/execution/safety.py` at the module level (before the class):

```python
from datetime import datetime, timedelta, timezone


def next_market_open() -> datetime:
    """Calculate the next market open (9:31 AM ET) as UTC datetime."""
    now = datetime.now(timezone.utc)
    
    # Approximate ET offset (conservative: use EST = UTC-5)
    # 9:31 AM ET = 14:31 UTC (EST) or 13:31 UTC (EDT)
    # Use 14:31 to be safe (market will definitely be open)
    target_hour_utc = 14
    target_minute_utc = 31
    
    # Start from tomorrow
    candidate = now.replace(
        hour=target_hour_utc, minute=target_minute_utc, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    
    # Skip weekends
    while candidate.weekday() >= 5:  # Saturday=5, Sunday=6
        candidate += timedelta(days=1)
    
    return candidate
```

- [ ] **Step 2: Modify executor to queue when market closed**

In `src/execution/executor.py`, find the safety check failure handling (around lines 173-213). We need to detect specifically when the failure is `market_hours` and queue instead of failing.

After the `if not safety_report.passed:` block (line 173), before the dry_run check, add market-closed queuing:

```python
        if not safety_report.passed:
            # Check if the ONLY failure is market_hours
            failures = safety_report.get_failures()
            market_closed_only = (
                len(failures) == 1
                and failures[0].check_name == "market_hours"
                and not self.dry_run
            )

            if market_closed_only:
                # Queue for next market open instead of failing
                try:
                    from src.db.repositories.state_repository import state_repository
                    from src.db.session import get_db_context
                    from src.execution.safety import next_market_open

                    execute_after = next_market_open()
                    async with get_db_context() as db:
                        queued = await state_repository.queue_execution(
                            db=db,
                            approval_id=approval_id,
                            trades=trades,
                            execute_after=execute_after,
                        )

                    log.info(
                        "trades_queued_for_market_open",
                        queued_id=str(queued.id),
                        execute_after=execute_after.isoformat(),
                    )

                    # Notify via Telegram
                    try:
                        from src.approval.telegram import get_telegram_bot
                        bot = get_telegram_bot()
                        symbols = ", ".join(t["symbol"] for t in trades)
                        await bot.send_notification(
                            f"*Trades Queued*\n\n"
                            f"Market is closed. {len(trades)} trade(s) ({symbols}) "
                            f"queued for execution at {execute_after.strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
                            f"Use /cancel\\_queued to cancel.",
                        )
                    except Exception as e:
                        log.warning("queued_notification_failed", error=str(e))

                    report.success = True
                    report.error = "queued_for_market_open"
                    report.completed_at = datetime.now(timezone.utc)
                    return report
                except Exception as e:
                    log.exception("failed_to_queue_trades", error=str(e))
                    # Fall through to normal failure handling

            log.warning(
                "safety_checks_failed",
                failures=[c.check_name for c in safety_report.get_failures()],
```

Important: the existing `log.warning("safety_checks_failed", ...)` and the rest of the safety failure handling stays in place as the fallback.

- [ ] **Step 3: Commit**

```bash
git add src/execution/executor.py src/execution/safety.py
git commit -m "feat: queue approved trades for market open when market is closed"
```

---

### Task 7: Execute-Queued Job Endpoint

**Files:**
- Modify: `src/api/main.py` (add new job endpoint)

- [ ] **Step 1: Add the execute-queued endpoint**

Add after the existing job endpoints in `src/api/main.py` (after the `expire-approvals` endpoint):

```python
@app.post("/jobs/execute-queued", response_model=JobResponse, tags=["Jobs"])
async def job_execute_queued():
    """
    Scheduled job: Execute trades queued for market open.

    Called by Cloud Scheduler at 9:31 AM ET on weekdays.
    """
    started_at = datetime.now(timezone.utc)
    log = structlog.get_logger(__name__).bind(job="execute_queued")

    try:
        log.info("starting_queued_execution")

        from src.db.repositories.state_repository import state_repository
        from src.db.session import get_db_context
        from src.execution.executor import execute_approved_trades

        async with get_db_context() as db:
            pending = await state_repository.get_pending_executions(
                db, before=datetime.now(timezone.utc)
            )

        if not pending:
            log.info("no_queued_executions")
            return JobResponse(
                job="execute_queued",
                status="completed",
                started_at=started_at.isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                results={"executed": 0, "queued_count": 0},
            )

        log.info("found_queued_executions", count=len(pending))

        executed = 0
        failed = 0
        results = []

        for queued in pending:
            try:
                # Mark as executing
                async with get_db_context() as db:
                    await state_repository.update_queued_status(
                        db, queued.id, "executing"
                    )

                # Execute the trades
                report = await execute_approved_trades(
                    approval_id=queued.approval_id,
                    trades=queued.trades,
                )

                if report.success:
                    async with get_db_context() as db:
                        await state_repository.update_queued_status(
                            db, queued.id, "completed"
                        )
                    executed += 1
                    results.append({
                        "queued_id": str(queued.id),
                        "success": True,
                        "executed": report.executed,
                    })
                else:
                    async with get_db_context() as db:
                        await state_repository.update_queued_status(
                            db, queued.id, "failed", error=report.error
                        )
                    failed += 1
                    results.append({
                        "queued_id": str(queued.id),
                        "success": False,
                        "error": report.error,
                    })

            except Exception as e:
                log.exception(
                    "queued_execution_failed",
                    queued_id=str(queued.id),
                    error=str(e),
                )
                async with get_db_context() as db:
                    await state_repository.update_queued_status(
                        db, queued.id, "failed", error=str(e)
                    )
                failed += 1
                results.append({
                    "queued_id": str(queued.id),
                    "success": False,
                    "error": str(e),
                })

        # Notify results
        try:
            from src.approval.telegram import get_telegram_bot
            bot = get_telegram_bot()
            await bot.send_notification(
                f"*Queued Execution Complete*\n\n"
                f"Executed: {executed}\n"
                f"Failed: {failed}",
            )
        except Exception:
            pass

        log.info("queued_execution_completed", executed=executed, failed=failed)

        return JobResponse(
            job="execute_queued",
            status="completed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            results={
                "queued_count": len(pending),
                "executed": executed,
                "failed": failed,
                "details": results,
            },
        )

    except Exception as e:
        log.exception("execute_queued_job_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
```

Note: this endpoint already uses proper error handling (raises HTTPException on failure) — that's the pattern for Task 8.

- [ ] **Step 2: Ensure HTTPException is imported**

Check `src/api/main.py` imports. Add if missing:

```python
from fastapi import HTTPException
```

- [ ] **Step 3: Commit**

```bash
git add src/api/main.py
git commit -m "feat: add /jobs/execute-queued endpoint for market-open execution"
```

---

### Task 8: Job Error Handling — Proper HTTP Status Codes

**Files:**
- Modify: `src/api/main.py` (change job endpoints to raise on failure)

- [ ] **Step 1: Update poll-email error handling**

In `src/api/main.py`, find the outer except block for `job_poll_email` (around line 407-415):

Old code:
```python
    except Exception as e:
        log.exception("email_poll_failed", error=str(e))
        return JobResponse(
            job="poll_email",
            status="failed",
            started_at=started_at.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
```

New code:
```python
    except Exception as e:
        log.exception("email_poll_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Email poll failed: {e}")
```

- [ ] **Step 2: Update all other job endpoints the same way**

Apply the same pattern to:
- `job_reconcile` (the outer except block)
- `job_poll_buffett` (the outer except block)
- `job_expire_approvals` (the outer except block)
- `job_poll_bravos` (the outer except block, if it exists)

For each, replace the `return JobResponse(status="failed", ...)` with `raise HTTPException(status_code=500, detail=...)`.

Keep the inner per-item error handling unchanged (individual email failures should still be caught and logged, not crash the whole job).

- [ ] **Step 3: Commit**

```bash
git add src/api/main.py
git commit -m "fix: job endpoints return HTTP 500 on failure for Cloud Scheduler retry"
```

---

### Task 9: Telegram /cancel_queued Command

**Files:**
- Modify: `src/approval/telegram.py` (add command handler)

- [ ] **Step 1: Register the command**

In `src/approval/telegram.py`, find where command handlers are registered (around line 121). Add:

```python
app.add_handler(CommandHandler("cancel_queued", self._cmd_cancel_queued))
```

- [ ] **Step 2: Implement the handler**

Add to the Login / MFA Commands section (or create a new section nearby):

```python
async def _cmd_cancel_queued(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel_queued command - cancel pending queued executions."""
    user = update.effective_user
    if not self._is_authorized(user.id):
        await update.message.reply_text("Unauthorized")
        return

    try:
        from src.db.repositories.state_repository import state_repository
        from src.db.session import get_db_context

        async with get_db_context() as db:
            pending = await state_repository.get_pending_executions(db)

            if not pending:
                await update.message.reply_text("No queued executions to cancel.")
                return

            # Show what will be cancelled
            lines = [f"*Cancelling {len(pending)} queued execution(s):*\n"]
            for q in pending:
                symbols = ", ".join(t.get("symbol", "?") for t in q.trades)
                lines.append(
                    f"  {symbols} — scheduled {q.execute_after.strftime('%Y-%m-%d %H:%M UTC')}"
                )

            count = await state_repository.cancel_pending_executions(db)

        lines.append(f"\n*Cancelled {count} execution(s).*")
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.exception("cancel_queued_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)}")
```

- [ ] **Step 3: Commit**

```bash
git add src/approval/telegram.py
git commit -m "feat: add /cancel_queued Telegram command"
```

---

### Task 10: Buffett 13F State — Migrate to Database

**Files:**
- Modify: `src/signals/buffett_processor.py` (or wherever 13F state is read/written)

- [ ] **Step 1: Find and update 13F state persistence**

Search for `buffett_13f_state.json` references:

```bash
grep -rn "buffett_13f_state\|13f_state" src/
```

Replace file reads/writes with database calls using the same pattern as entry prices:

```python
from src.db.repositories.state_repository import state_repository
from src.db.session import get_db_context

# Reading state
async with get_db_context() as db:
    state = await state_repository.get_state(db, "buffett_13f_state")
    last_accession = state.get("last_accession") if state else None

# Writing state
async with get_db_context() as db:
    await state_repository.set_state(db, "buffett_13f_state", {
        "last_accession": accession_number,
        "report_date": report_date,
    })
```

The exact code depends on the file structure — adapt to match the existing pattern.

- [ ] **Step 2: Commit**

```bash
git add src/signals/buffett_processor.py  # or whichever file changed
git commit -m "feat: migrate Buffett 13F state from JSON file to database"
```

---

### Task 11: Deploy and Verify

**Files:** No code changes — deployment and infrastructure.

- [ ] **Step 1: Create Cloud Scheduler job**

```bash
gcloud scheduler jobs create http execute-queued-trades \
    --location us-central1 \
    --schedule "31 14 * * 1-5" \
    --uri "https://investing-orchestrator-770951850070.us-central1.run.app/jobs/execute-queued" \
    --http-method POST \
    --attempt-deadline 120s \
    --max-retry-attempts 3 \
    --min-backoff 30s
```

Note: `31 14 * * 1-5` = 14:31 UTC = 9:31 AM EST on weekdays.

- [ ] **Step 2: Grant IAM permission for Secret Manager writes**

```bash
gcloud projects add-iam-policy-binding investing-automation-490206 \
    --member="serviceAccount:770951850070-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretVersionManager"
```

Verify the service account email matches your Cloud Run service account. Check with:
```bash
gcloud run services describe investing-orchestrator --region us-central1 --format="value(spec.template.spec.serviceAccountName)"
```

- [ ] **Step 3: Deploy**

```bash
gcloud run deploy investing-orchestrator --source . --region us-central1
```

- [ ] **Step 4: Verify state persistence**

After deploy, check logs for:
- `bravos_entry_prices_loaded` — entry prices loading from DB
- `loaded_processed_ids` — email dedup working
- `gmail_token_persisted_to_secret_manager` — after next token refresh

Test queued execution by approving a trade after market hours — should see "Trades Queued" notification.

- [ ] **Step 5: Clean up old files**

Once verified, remove the now-unused state files from the repo:

```bash
git rm data/state/bravos_entry_prices.json
git commit -m "chore: remove bravos_entry_prices.json (migrated to database)"
```
