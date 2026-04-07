# System Reliability: Database-Centric State & High-Priority Fixes

**Date:** 2026-04-06
**Status:** Approved
**Approach:** A — Database-centric (all state in Supabase PostgreSQL)

## Problem

Cloud Run containers are ephemeral. The system currently stores critical state on the filesystem (`data/state/`, `data/sessions/`), which is lost on every cold start. This causes:

- Duplicate signal processing (email poll state lost)
- Missing price context in trade messages (entry prices lost)
- Gmail token expiry after 7 days (refreshed token not persisted)
- Approved off-hours trades silently dropped (no queuing mechanism)
- Silent job failures (HTTP 200 returned on errors)

## Scope

Five high-priority fixes, all using the existing Supabase database as the single source of truth.

1. Persist all runtime state to database
2. Gmail token refresh → write back to Secret Manager
3. Off-hours order queuing with market-open execution
4. Job endpoint error handling (proper HTTP status codes)
5. Email deduplication via signals table query

## Data Model

### New Tables

```sql
-- Simple key-value store for small state blobs
CREATE TABLE key_value_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bravos entry prices — write-once per symbol
CREATE TABLE entry_prices (
    symbol TEXT PRIMARY KEY,
    price NUMERIC NOT NULL,
    source TEXT NOT NULL,          -- 'bravos_scrape' | 'manual' | 'cache_import'
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Approved trades waiting for market open
CREATE TABLE queued_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_id UUID NOT NULL REFERENCES approvals(id),
    trades JSONB NOT NULL,         -- Array of trade dicts from the approval
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    execute_after TIMESTAMPTZ NOT NULL,  -- Next market open (9:31 AM ET)
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | executing | completed | failed
    executed_at TIMESTAMPTZ,
    error TEXT
);
```

### key_value_state Keys

| Key | Value Shape | Purpose |
|-----|-------------|---------|
| `email_poll_state` | `{"last_processed_message_id": str, "last_checked_at": str}` | Email polling cursor |
| `buffett_13f_state` | `{"last_accession": str, "report_date": str}` | 13F filing tracking |
| `gmail_token` | `{"token_json": str, "refreshed_at": str}` | Backup of refreshed Gmail token |

### entry_prices Behavior

Write-once: when a new Bravos symbol appears, insert its entry price. Never overwrite. If Bravos removes the position later, the price persists. Seeded from existing `bravos_entry_prices.json` on first deploy.

## Component Changes

### A. State Persistence Layer — new `src/db/state.py`

- `get_state(key) -> dict | None` and `set_state(key, value)` wrapping `key_value_state`
- `get_entry_price(symbol) -> float | None` and `upsert_entry_price(symbol, price, source)` (insert only if not exists)
- `get_pending_queued_executions() -> list[QueuedExecution]` and status update methods
- All async, using existing database session pattern from `src/db/repository.py`

### B. Email Deduplication — `src/signals/bravos_processor.py` + `src/api/main.py`

- At poll time, query `signals` table for existing `source_event_id`s
- Filter out already-processed message IDs before processing
- Remove filesystem `bravos_email_state.json` dependency
- Write `last_checked_at` to `key_value_state` for observability

### C. Gmail Token Refresh — `src/signals/email_monitor.py`

- After successful `creds.refresh()`, write refreshed token JSON to `key_value_state` (backup)
- Update Secret Manager via `google.cloud.secretmanager` API: add new version of `GMAIL_TOKEN_JSON`
- Requires `secretmanager.versions.add` IAM permission on Cloud Run service account
- Startup fallback chain: Secret Manager -> `key_value_state` -> fail with Telegram notification

### D. Off-Hours Queuing — `src/execution/executor.py` + new job endpoint

- When safety check finds market closed: insert into `queued_executions` with `execute_after` = next market open (9:31 AM ET next trading day)
- Send Telegram notification: "Trade queued for market open at 9:31 AM ET"
- New endpoint `POST /jobs/execute-queued`: Cloud Scheduler calls at 9:31 AM ET on weekdays
  - Fetch pending queued executions where `execute_after <= now()`
  - Re-check price deviation against `proposal_price`
  - If deviation > threshold (2%): notify user via Telegram, mark failed
  - If OK: execute normally, mark completed
- Holiday handling: attempt execution; if Robinhood rejects (market closed), bump `execute_after` to next weekday
- New `/cancel-queued` Telegram command to clear pending executions before market open

### E. Job Error Handling — `src/api/main.py`

- Let exceptions propagate -> FastAPI returns 500 -> Cloud Scheduler sees failure
- Return 503 for transient errors (network timeout, rate limit) — Cloud Scheduler retries
- Return 500 for permanent errors (bad credentials, missing data) — alert, don't retry
- Keep structured logging on all errors
- Distinguish "nothing to do" (200) from "something broke" (5xx)
- Recommended Cloud Scheduler retry policy: max 3 retries, 30s initial backoff, 2x multiplier

## Error Handling & Edge Cases

### Off-Hours Queuing

- **Price moves overnight**: `execute-queued` job re-checks price deviation before executing. If > 2% threshold, marks failed and notifies user to re-approve.
- **Multiple queued batches**: Processed in FIFO order by `queued_at`.
- **Holiday detection**: Attempt execution; if rejected, bump to next weekday. No holiday calendar needed.
- **User cancels overnight**: `/cancel-queued` Telegram command shows and clears pending executions.
- **Duplicate prevention**: `approval_id` prevents the same approval from being queued twice.

### Gmail Token

- **Secret Manager write fails**: Fall back to `key_value_state` only. Log warning. System works until DB token also expires.
- **Token irrecoverably expired** (>7 days without refresh, refresh token revoked): Telegram notification with manual re-auth instructions. Unavoidable — Google requires periodic human consent.
- **Race condition on refresh**: Check `key_value_state` `updated_at` — if refreshed within last 30 minutes, skip.

### Email Deduplication

- **First run after migration**: No signals in database yet. All emails processed — same as today.
- **Gmail API rate limit**: Already handled by existing error catching.

### Entry Prices

- **Symbol renamed/split**: Entry price stays under old symbol. New symbol gets its own entry. Stock splits need manual adjustment (rare).
- **Migration**: Seed `entry_prices` table from `bravos_entry_prices.json` on first deploy. Remove JSON file and Dockerfile COPY line after.

## Files to Modify

| File | Change |
|------|--------|
| `src/db/state.py` | **New** — state persistence layer |
| `src/db/models.py` | Add SQLAlchemy models for new tables |
| `src/signals/bravos_processor.py` | Use `entry_prices` table; remove JSON file dependency |
| `src/signals/email_monitor.py` | Gmail token refresh -> Secret Manager + key_value_state |
| `src/api/main.py` | Email dedup via signals query; new `/jobs/execute-queued` endpoint; proper HTTP error codes |
| `src/execution/executor.py` | Queue trades when market closed instead of blocking |
| `src/approval/telegram.py` | Add `/cancel-queued` command; price context uses DB entry prices |
| `cloudbuild.yaml` | Add `secretmanager.versions.add` IAM note |
| `Dockerfile` | Remove `COPY data/state/bravos_entry_prices.json` line |
| `requirements.txt` | Add `google-cloud-secret-manager` |

## Cost Impact

Negligible (~$0 additional):
- Database: tiny tables, 5-10 extra queries per poll cycle
- Secret Manager: ~1 write/month for Gmail token refresh
- Cloud Scheduler: 1 new job ($0.10/month)
- Cloud Run: 1 additional cold start per trading day morning

One IAM change required: `Secret Manager Secret Version Manager` role on Cloud Run service account.

## Migration Plan

1. Run SQL migration to create tables in Supabase
2. Seed `entry_prices` from existing `bravos_entry_prices.json`
3. Deploy new code
4. Verify state persists across container restarts
5. Remove `bravos_entry_prices.json` from Dockerfile and repo
6. Add Cloud Scheduler job for `POST /jobs/execute-queued` at 9:31 AM ET weekdays
7. Grant IAM permission for Secret Manager writes
