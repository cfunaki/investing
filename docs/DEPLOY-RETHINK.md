# Deploy runbook: security rethink + Bravos retirement

Branch: `feature/security-rethink` (from `origin/main`).
Review the diff, merge to `main`, then follow this runbook in order.
Do not skip step 1.

## What changed

| Area | Change |
|---|---|
| Auth | New `src/api/auth.py`: `X-API-Key` middleware. `API_KEY` = admin (all `/admin`, `/trigger`, `/debug`, `/jobs`). `AI_API_KEY` = scoped (position/sleeve reads + `custom-buys`/`custom-sells` submit only; 30 req/hr, $50k/day notional cap). Telegram webhook validated via `TELEGRAM_WEBHOOK_SECRET` (`X-Telegram-Bot-Api-Secret-Token`). |
| Ledger bug | `src/approval/workflow.py`: `exec_result.quantity` → `filled_quantity`, `fill_price` → `filled_price`. This AttributeError aborted the whole update before commit on every run — the root cause of the duplicate buys. Per-trade try/except added so one bad symbol can't kill the batch. |
| custom-buys | New `POST /admin/sleeve/{name}/custom-buys` (`{SYMBOL: dollars}`). Guardrails: buying-power check (never cash-negative), tactical-sleeve 10% cap, auto-split over `MAX_TRADE_NOTIONAL`, AI daily notional cap. |
| custom-sells | Now validates requested shares against live Robinhood positions; rejects phantom sells with 400 instead of failing at the broker. |
| Telegram | Sell lines show `-N sh (price n/a)` instead of `-$0` when the price lookup fails. `setup_webhook` accepts `secret_token`. |
| Bravos | `POST /jobs/poll-bravos` and `POST /jobs/poll-email` now return 410. The scraper, email monitor, and processor modules are still in the tree but unwired. |
| Sleeves | Migration `006_tactical_sleeve.sql` creates the `tactical` sleeve (AI-submitted, owner-approved). Bravos sleeve is left alone until wind-down completes. |

## Step 1 — Secrets (before deploy)

Create three Secret Manager secrets (generate with
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`):

- `API_KEY` — admin key. Only you hold this.
- `AI_API_KEY` — scoped key for the AI assistant. Share via secure vault only.
- `TELEGRAM_WEBHOOK_SECRET` — any random string.

Add them to the orchestrator `--set-secrets` line in `cloudbuild.yaml`:

```
--set-secrets
- 'DATABASE_URL=<redacted>,API_KEY=API_KEY:latest,AI_API_KEY=AI_API_KEY:latest,TELEGRAM_WEBHOOK_SECRET=TELEGRAM_WEBHOOK_SECRET:latest'
```

## Step 2 — Deploy

```bash
gcloud builds submit --config cloudbuild.yaml --region=us-central1
```

On startup the app runs migrations (creates `tactical` sleeve) and calls
`set_webhook` with the secret token, which makes Telegram start sending the
validation header.

## Step 3 — Verify auth (do this before anything else)

```bash
BASE=https://investing-orchestrator-<PROJECT#>.us-central1.run.app

# No key -> 401
curl -s -o /dev/null -w "%{http_code}\n" $BASE/admin/sleeve/tactical

# Wrong key -> 401
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: wrong" $BASE/admin/sleeve/tactical

# Admin key -> 200
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: $API_KEY" $BASE/admin/sleeve/tactical

# AI key on execute endpoint -> 403 (scoped out)
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "X-API-Key: $AI_API_KEY" \
  $BASE/admin/approval/00000000-0000-0000-0000-000000000000/execute
```

Expected: `401`, `401`, `200`, `403`.

## Step 4 — Cloud Scheduler updates

Every job that calls `/jobs/*` must now send the admin key, or the calls
start failing with 401 (this includes `execute-queued` — the Monday-morning
runner — so do this promptly after deploy):

```bash
for JOB in <your-job-names>; do
  gcloud scheduler jobs update http $JOB --location=us-central1 \
    --headers="X-API-Key=$API_KEY"
done
```

Delete the Bravos jobs entirely (their endpoints are 410 now):

```bash
gcloud scheduler jobs delete bravos-email-check --location=us-central1 --quiet
gcloud scheduler jobs delete <any-other-bravos-job> --location=us-central1 --quiet
```

(`buffett-13f-check` stays — the Buffett sleeve is unchanged.)

## Step 5 — Resync the ledger (one time)

The ledger has never successfully updated. After the wind-down trades
execute, resync it from Robinhood truth:

```bash
curl -X POST -H "X-API-Key: $API_KEY" \
  $BASE/admin/sleeve/tactical/positions/sync-from-rh
# repeat for buffett
curl -X POST -H "X-API-Key: $API_KEY" \
  $BASE/admin/sleeve/buffett/positions/sync-from-rh
```

Then disable the bravos sleeve (only after its positions are flat):

```sql
UPDATE sleeves SET enabled = false WHERE name = 'bravos';
```

## Step 6 — Give the AI assistant access

Share via the secure vault (never chat, never git):
1. The service base URL.
2. The `AI_API_KEY` value.

The assistant verifies with `GET /debug/positions` (read-only), then can
submit via `POST /admin/sleeve/tactical/custom-buys` and `custom-sells`.
Every submission still requires your Telegram approval — the assistant
cannot execute, cancel, or change config.

## Step 7 — Monday verification (after the queued rebalance executes)

- [ ] Queued trades executed sells before buys; margin debt cleared.
- [ ] `GET /debug/positions` matches the Robinhood app.
- [ ] No duplicate-buy proposals in the next Telegram message.
- [ ] Scheduler jobs (`execute-queued`, `expire-approvals`) ran with 200s.

## Notes / residual risks

- `--allow-unauthenticated` stays in `cloudbuild.yaml`; the app-level key is
  now the real gate (health checks need no auth). Removing the IAM binding
  entirely is optional defense-in-depth — it would require giving Cloud
  Scheduler a service account with `roles/run.invoker`.
- The AI rate limiter is in-memory per instance; fine for bounding a buggy
  caller, not a security boundary. The key scope + notional cap are the
  boundary.
- `custom-buys`/`custom-sells` write `target_weight: 0` so the ledger does
  not adopt non-sleeve symbols as sleeve positions (matches existing
  convention).
- Wash-sale caution stands: several lots were bought within 30 days. Prefer
  selling the specific oldest lots at the broker when trimming losers.
