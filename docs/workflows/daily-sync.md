# Daily Sync Workflow

Synchronize your broker portfolio with a sleeve's target allocations.

## Overview

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ Fetch Sleeve │────▶│  Reconcile   │────▶│   Execute    │
│   Targets    │     │   vs Broker  │     │   (Optional) │
└──────────────┘     └──────────────┘     └──────────────┘
```

## Steps at a Glance

1. Fetch latest targets from sleeve
2. Fetch current broker holdings
3. Run reconciliation
4. Review proposed changes
5. Execute (optional)

## Detailed Steps

### Step 1: Fetch Sleeve Targets

**Bravos Sleeve:**
```bash
npm run scrape-active
```

**Custom CSV Sleeve:**
```bash
# Targets already in data/raw/my-sleeve.csv
.venv/bin/python -m src.adapters.csv_sleeve
```

**Verification:**
```bash
cat data/processed/target_allocations.json | head -20
```

### Step 2: Fetch Broker Holdings

```bash
.venv/bin/python -m src.robinhood.holdings
```

**Verification:**
```bash
cat data/processed/robinhood_holdings.json | jq '.holdings | length'
```

### Step 3: Run Reconciliation

**Standard (fixed-dollar):**
```bash
.venv/bin/python -m src.recon.reconcile --fixed-dollar 500
```

**Price-adjusted (for catch-up trades):**
```bash
.venv/bin/python -m src.recon.reconcile --price-adjusted 500 0.30
```

**Output example:**
```
============================================================
RECONCILIATION SUMMARY
============================================================
  Target Bravos value: $    31,000
  ────────────────────────────────────────
  Total to SELL:       $    16,210
  Total to BUY:        $     9,921
  Net cash flow:       $    +6,290
============================================================

📤 SELL / EXIT:
  EXIT  GE     $   3,392  (Not in Bravos targets)
  SELL  AA     $   3,353  (trim to target)

📥 BUY / ENTER:
  ENTER NEE    $   2,500  (new position)
  BUY   EME    $     421  (add to position)
```

### Step 4: Review Proposed Changes

Check the reconciliation output:

```bash
# Summary view
cat data/processed/reconciliation.json | jq '.summary'

# Detailed deltas
cat data/processed/reconciliation.json | jq '.deltas[] | {symbol, action, suggested_trade_value, notes}'
```

**Questions to ask:**
- Are the actions sensible? (exits for non-sleeve stocks, enters for new positions)
- Is the net cash flow reasonable?
- Any unexpected large trades?

### Step 5: Execute Trades

**Dry-run first:**
```bash
.venv/bin/python -m src.trading.execute_trades --dry-run
```

**Live execution:**
```bash
.venv/bin/python -m src.trading.execute_trades --live
```

With per-trade confirmation (interactive):
```bash
.venv/bin/python -m src.trading.execute_trades --live
# Answer y/n/q for each trade
```

Without prompts (non-interactive):
```bash
.venv/bin/python -m src.trading.execute_trades --live --no-confirm
```

## Quick Commands by Sleeve

### Bravos

**Full sync (Claude command):**
```
/sync-bravos
```

**Manual:**
```bash
npm run scrape-active && npm run reconcile
.venv/bin/python -m src.trading.execute_trades --live
```

### Custom Sleeve

```bash
# Update your CSV/source
.venv/bin/python -m src.adapters.my_sleeve
.venv/bin/python -m src.recon.reconcile --fixed-dollar 500
.venv/bin/python -m src.trading.execute_trades --live
```

## Validation

After execution, verify:

1. **Check pending orders:**
   ```bash
   .venv/bin/python -m src.trading.execute_trades --pending
   ```

2. **Re-fetch holdings:**
   ```bash
   .venv/bin/python -m src.robinhood.holdings
   ```

3. **Re-run reconciliation (should show minimal deltas):**
   ```bash
   .venv/bin/python -m src.recon.reconcile --fixed-dollar 500
   ```

## Scheduling

For automated daily syncs, see [Cloud Deployment](cloud-deployment.md).

Manual cron example:
```bash
# Run at 9:35 AM ET (after market open)
35 9 * * 1-5 cd /path/to/investing && npm run scrape-active && npm run reconcile
```
