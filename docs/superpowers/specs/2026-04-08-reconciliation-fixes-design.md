# Reconciliation Pipeline Fixes

**Goal:** Fix five bugs causing incorrect trade proposals, duplicate approvals, and poll failures.

**Architecture:** Targeted fixes to the existing reconciliation pipeline — no new services or tables. Bootstrap the virtual ledger, fix sell calculations, fix email dedup timing, remove auto-retrigger spam, and fix stale Gmail connections.

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

## Fix 2: Sell Calculations — Use Shares, Not Notional

**What:** Change `_generate_trades()` in `delta_reconciler.py` to use actual share quantities for sells instead of `weight_delta × unit_size`.

**Problem:** Currently, sells use the same `notional = delta × unit_size` formula as buys. This is wrong because share prices change after purchase. If you bought $2,500 of LIN at $487/share (5.13 shares) and it's now $550/share, a full exit should sell all 5.13 shares ($2,822), not $2,500 notional.

**New logic:**

- **Buy (enter/increase):** unchanged. `notional = weight_delta × unit_size`. Buys are dollar-amount based.
- **Sell — full exit (weight → 0):** `quantity = sleeve_position.shares` (sell everything).
- **Sell — decrease (weight 5 → 3):** `quantity = shares × (old_weight - new_weight) / old_weight`. Proportional to weight reduction. Example: own 5.13 shares at weight 5, reducing to weight 3 → sell 5.13 × (5-3)/5 = 2.05 shares.

**Trade object changes:** For sells, the `DeltaTrade` should carry `quantity` (shares) instead of `notional`. The executor already supports both `quantity` and `notional` on `OrderRequest`.

**Ledger is already correct:** `update_ledger_after_trades()` already uses actual fill data (shares from broker response) to update the ledger, not theoretical amounts.

## Fix 3: Email Dedup — Mark Early

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

## Fix 4: Remove Auto-Retrigger

**What:** Remove the auto-retrigger logic from `expire_approvals`.

**Current behavior:** When approvals expire, the system creates new approvals with the same trades and sends new Telegram messages.

**New behavior:** Expired approvals just expire. The next email poll or manual reconciliation will create a fresh approval with updated prices if the portfolio is still out of sync.

**Code changes:**
- Remove `_retrigger_approval()` method from `workflow.py`
- Remove the retrigger loop from the expire flow — keep only the "mark as expired" logic

## Fix 5: Gmail Broken Pipe

**What:** Don't cache the Gmail service object across poll calls.

**Root cause:** The Gmail API HTTP connection goes stale between 30-minute polling intervals. The next call writes to a dead socket → `[Errno 32] Broken pipe`.

**Fix:** Clear `self._service = None` at the start of each `check_for_emails()` call, forcing `_get_service()` to rebuild a fresh connection. Credential caching (token refresh) is unaffected — only the HTTP transport is rebuilt.
