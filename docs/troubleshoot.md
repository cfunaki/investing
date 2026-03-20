# Troubleshooting

Common issues and their solutions.

## Symptom Routing Table

| Symptom | Likely Area | Quick Fix |
|---------|-------------|-----------|
| "Login failed" | Broker auth | Check .env credentials |
| "No targets found" | Sleeve session | Re-authenticate sleeve |
| "Rate limited (429)" | Broker API | Wait 60s, retry |
| Fractional sells fail | API issue | Uses dollar-based sells |
| "Symbol not found" | Regional restriction | Skip or find alternative |
| Orders not queued | Market closed | Wait for market hours |
| Reconciliation empty | No allocations | Check target_allocations.json |
| "Session expired" | Sleeve auth | Re-run init-session |

## Rapid Debug Loop

When something goes wrong, check these in order:

### 1. Verify Credentials

```bash
# Check .env exists and has values
cat .env | grep -E '^(RH_|BRAVOS_)' | head -5
```

### 2. Test Broker Connection

```bash
.venv/bin/python -m src.robinhood.auth
```

Expected: `Login successful!`

### 3. Verify Sleeve Session

```bash
# Check session file exists
ls -la data/sessions/

# For Bravos, check cookies are present
cat data/sessions/bravos.json | jq '.cookies | length'
```

### 4. Check Data Pipeline

```bash
# Targets exist?
cat data/processed/target_allocations.json | jq '.allocations | length'

# Holdings exist?
cat data/processed/robinhood_holdings.json | jq '.holdings | length'

# Reconciliation ran?
cat data/processed/reconciliation.json | jq '.summary'
```

### 5. Check Pending Orders

```bash
.venv/bin/python -m src.trading.execute_trades --pending
```

---

## Authentication Issues

### Robinhood Login Failed

**Symptom:**
```
robin_stocks.robinhood.authentication: ERROR - Bad Request: challenge_request...
```

**Causes:**
1. Wrong password
2. 2FA required but TOTP secret not configured
3. Account locked from too many attempts

**Solutions:**
1. Verify `RH_USERNAME` and `RH_PASSWORD` in .env
2. Add `RH_TOTP_SECRET` (base32 encoded from authenticator app setup)
3. Wait 15 minutes, then try again

### Bravos Session Expired

**Symptom:**
```
Error: Session expired or invalid
```

**Solution:**
```bash
npm run init-session
```

### Bravos Login Fails

**Symptom:**
```
Error: Login failed - check credentials
```

**Solutions:**
1. Verify `BRAVOS_USERNAME` and `BRAVOS_PASSWORD` in .env
2. Try logging in manually at bravosresearch.com to verify credentials work
3. Check if Bravos site is down

---

## Scraping Issues

### No Active Trades Found

**Symptom:**
```
Found 0 active trades
```

**Causes:**
1. Session expired
2. Bravos site structure changed
3. No trades currently active

**Solutions:**
1. Re-authenticate: `npm run init-session`
2. Check Bravos manually to see if trades exist
3. Review `scripts/scrape-active-trades.ts` for selector changes

### Scrape Returns Old Data

**Symptom:** Data from previous day

**Solution:** Clear raw data and re-scrape:
```bash
rm data/raw/active-trades-*.json
npm run scrape-active
```

---

## Reconciliation Issues

### No Deltas Generated

**Symptom:** Reconciliation shows no trades needed

**Causes:**
1. Portfolio already aligned
2. All positions within $50 tolerance
3. Target allocations empty

**Debug:**
```bash
# Check target allocations
cat data/processed/target_allocations.json | jq '.allocations[0]'

# Check holdings
cat data/processed/robinhood_holdings.json | jq '.holdings[0]'
```

### Wrong Target Amounts

**Symptom:** Suggested trades don't match expected

**Causes:**
1. Wrong `--fixed-dollar` value
2. Using percentage mode instead of fixed-dollar
3. Price adjustment enabled unexpectedly

**Debug:**
```bash
# Check what mode was used
cat data/processed/reconciliation.json | jq '.summary.dollars_per_weight'
```

---

## Trade Execution Issues

### Rate Limited (429)

**Symptom:**
```
Error in request_post: Received 429
```

**Solution:**
1. Wait 60 seconds
2. Check what executed: `--pending`
3. Retry remaining trades

### Fractional Sells Return None

**Symptom:**
```
ERROR: {'status': 'error', 'error': 'API returned None', 'symbol': 'AA'}
```

**Cause:** Robinhood's fractional sell API is flaky

**Solution:** The system automatically falls back to dollar-based sells. If still failing:
```bash
# Sell manually via Robinhood app, then re-run reconciliation
```

### Symbol Not Found

**Symptom:**
```
400 Client Error: Bad Request for url: .../quotes/?symbols=ALUM
```

**Cause:** Symbol not available on Robinhood (e.g., European ETFs)

**Solution:**
1. Skip this trade (it will be logged as an error)
2. Find US-listed alternative
3. Use a different broker (e.g., Interactive Brokers)

### "Not Enough Shares to Sell"

**Symptom:**
```
{'detail': 'Not enough shares to sell.', 'symbol': 'GE'}
```

**Cause:** Shares already sold (order executed previously)

**Solution:** This is expected if re-running execution. The order already went through.

---

## Data Issues

### Missing Entry Prices

**Symptom:** Price adjustment shows "No entry price" for all symbols

**Solution:** Run the detailed Bravos scraper:
```bash
npx tsx scripts/scrape-bravos-trades.ts
```

### Stale Holdings Data

**Symptom:** Holdings show positions that were sold

**Solution:**
```bash
.venv/bin/python -m src.robinhood.holdings
```

### Corrupted JSON

**Symptom:**
```
json.decoder.JSONDecodeError: ...
```

**Solution:**
```bash
# Remove and regenerate
rm data/processed/*.json
npm run reconcile
```

---

## Cloud Deployment Issues

See [Cloud Deployment](workflows/cloud-deployment.md) troubleshooting section.

Quick checks:
```bash
# Check Cloud Run logs
gcloud run services logs read investing-orchestrator --region=us-central1

# Verify secrets
gcloud secrets versions access latest --secret=rh-username
```

---

## Getting Help

If issues persist:

1. Check logs: `data/trades/trades-*.json`
2. Enable debug logging: `LOG_LEVEL=DEBUG` in .env
3. Review recent changes to sleeve data or broker API
