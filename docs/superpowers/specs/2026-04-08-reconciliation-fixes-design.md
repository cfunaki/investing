# Reconciliation Pipeline Fixes

**Goal:** Fix four bugs causing incorrect trade proposals, duplicate approvals, and poll failures.

**Architecture:** Targeted fixes to the existing reconciliation pipeline — no new services or tables. Bootstrap the virtual ledger, fix email dedup timing, remove auto-retrigger spam, and fix stale Gmail connections.

## Problem Summary

1. **Empty ledger → buy-only trades.** The `delta_reconciler` compares Bravos targets against `sleeve_positions`, which is empty. Every position looks "new" → $20-28K of buy-only proposals.
2. **Email dedup race condition.** Emails are marked "processed" only after the full reconciliation completes. The next poll (30 min later) re-discovers the same email and creates duplicate approvals. 5 duplicate approval sets were created on 2026-04-08.
3. **Auto-retrigger spam.** Expired approvals auto-create new approvals with the same stale trades, compounding the duplicate problem.
4. **Broken pipe errors.** Half of email polls fail with `[Errno 32] Broken pipe` due to stale Gmail API connections cached across 30-minute polling intervals.

## Fix 1: Ledger Bootstrap

**What:** Seed `sleeve_positions` by cross-referencing Bravos targets with actual Robinhood holdings.

**How:**
1. Fetch current Bravos target portfolio → get set of target symbols
2. Fetch all RH holdings via `broker.get_positions()`
3. For each symbol in both Bravos targets AND RH holdings: create/update `sleeve_positions` row with actual RH quantity, market value, and Bravos target weight
4. Symbols in RH but not in Bravos → ignored (non-Bravos positions)
5. Symbols in Bravos but not in RH → no row (weight 0, correctly triggers buy on next reconciliation)

**When it runs:**
- On app startup (after migrations, after RH session restore) — only if the ledger is empty (first-time bootstrap)
- On-demand via `/sync` Telegram command (always runs, overwrites with current values)

**Idempotent:** Running bootstrap twice just overwrites with current values.

## Fix 2: Email Dedup — Mark Early

**What:** Mark emails as "processing" immediately on detection, before reconciliation starts.

**Current flow:** detect email → reconcile → approve → mark "processed"
**New flow:** detect email → **mark "processing"** → reconcile → approve → update to "processed"

**Details:**
- Insert signal with status `"processing"` before calling `_process_reconciliation()`
- `get_processed_message_ids()` already filters for `["processed", "processing", "skipped"]`, so subsequent polls will skip the email immediately
- On success: update signal status to `"processed"`
- On transient failure (broken pipe, timeout, connection error): increment `retry_count` on the signal, set status to `"retry"` so the next poll re-discovers it
- On persistent failure or retry_count >= 3: set status to `"failed"`, send Telegram notification: "Bravos email processing failed after 3 attempts: {error}"
- `"retry"` status is NOT in the `get_processed_message_ids()` filter, so the next poll will pick it up

**Retry classification:**
- Transient (retryable): `BrokenPipeError`, `ConnectionError`, `TimeoutError`, `OSError`
- Persistent (not retryable): `ValueError`, `ValidationError`, scrape parse failures

## Fix 3: Remove Auto-Retrigger

**What:** Remove the auto-retrigger logic from `expire_approvals`.

**Current behavior:** When approvals expire, the system creates new approvals with the same trades and sends new Telegram messages.

**New behavior:** Expired approvals just expire. The next email poll or manual reconciliation will create a fresh approval with updated prices if the portfolio is still out of sync.

**Code changes:**
- Remove `_retrigger_approval()` method from `workflow.py`
- Remove the retrigger loop from the expire flow — keep only the "mark as expired" logic

## Fix 4: Gmail Broken Pipe

**What:** Don't cache the Gmail service object across poll calls.

**Root cause:** The Gmail API HTTP connection goes stale between 30-minute polling intervals. The next call writes to a dead socket → `[Errno 32] Broken pipe`.

**Fix:** Clear `self._service = None` at the start of each `check_for_emails()` call, forcing `_get_service()` to rebuild a fresh connection. Credential caching (token refresh) is unaffected — only the HTTP transport is rebuilt.
