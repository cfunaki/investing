# Bravos Research Sleeve

[Bravos Research](https://bravosresearch.com) is a trading research service that provides active trade ideas with entry prices, stop losses, and position weights.

## Setup

### 1. Configure Credentials

Add to `.env`:

```env
BRAVOS_BASE_URL=https://bravosresearch.com
BRAVOS_USERNAME=your_bravos_email
BRAVOS_PASSWORD=your_bravos_password
```

### 2. Initialize Browser Session

The scraper uses Playwright to log in and persist the session:

```bash
npm run init-session
```

This will:
1. Open a headless browser
2. Navigate to Bravos login page
3. Authenticate with your credentials
4. Save cookies to `data/sessions/bravos.json`

**Verification**: Check that the session file exists:
```bash
ls -la data/sessions/bravos.json
```

### 3. Test Scraping

```bash
npm run scrape-active
```

Expected output:
```
Loaded existing session
Navigating to active trades...
Found 12 active trades
Saved to data/raw/active-trades-latest.json
```

## Data Captured

The Bravos scraper extracts:

| Field | Description | Example |
|-------|-------------|---------|
| `symbol` | Ticker symbol | EME |
| `companyName` | Company name | EMCOR Group |
| `weight` | Current position weight | 3 |
| `side` | Long or short | long |
| `entryPrice` | Original entry price | $687.73 |
| `currentStop` | Current stop loss | $705 |
| `targetPrice` | Target price (if stated) | null |
| `posts` | Array of trade updates | [...] |

### Comprehensive Trade Data

The detailed scraper (`scripts/scrape-bravos-trades.ts`) visits each trade's posts to extract:

- Entry price (from initial post or referenced in updates)
- Stop loss history (original and trailing)
- Weight changes (adds/trims)
- Partial profit prices
- Trade rationale

Output: `data/processed/bravos_trades.json`

## Usage

### Quick Sync (Claude Command)

```
/sync-bravos
```

This runs the full workflow:
1. Scrape active trades
2. Normalize and derive allocations
3. Fetch Robinhood holdings
4. Reconcile at $500/weight
5. Show proposed trades
6. Execute on confirmation

### Manual Sync

```bash
# 1. Scrape latest trades
npm run scrape-active

# 2. Run full reconciliation pipeline
npm run reconcile

# 3. Review output
cat data/processed/reconciliation.json

# 4. Execute (with confirmation)
.venv/bin/python -m src.trading.execute_trades --live
```

### Price-Adjusted Sync

For catch-up trades where you're entering positions late:

```bash
.venv/bin/python -m src.recon.reconcile --price-adjusted 500 0.30
```

This reduces allocation for positions that have already run up toward their targets:
- `500`: Dollars per weight
- `0.30`: Default 30% target upside assumption

## Bravos-Specific Considerations

### European ETFs

Some Bravos trades use European-listed ETFs (e.g., `ALUM` for aluminum). These are **not available on Robinhood**.

The system will:
1. Attempt to buy
2. Receive "Symbol not found" error
3. Log the failure
4. Continue with other trades

**Workaround**: Find US-listed alternatives or use Interactive Brokers.

### Trailing Stops

Bravos often raises stops after taking partial profits. The `currentStop` field reflects the **latest** stop, not the original.

This affects price-adjustment calculations:
- If stop > entry: Trailing stop (can't infer original risk)
- If stop = entry: Breakeven stop
- If stop < entry: Valid for risk/reward calculation

The system falls back to a default target percentage when stops are invalid.

### Weight Changes

Bravos adjusts position weights over time:
- **Add**: Increase exposure (e.g., 2 → 6)
- **Trim**: Take partial profits (e.g., 5 → 4)
- **Exit**: Close position entirely

The reconciliation uses the **current weight** from the most recent update.

### Session Expiry

The Bravos session typically lasts 2 weeks. If scraping fails:

```
Error: Session expired or invalid
```

Re-authenticate:
```bash
npm run init-session
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No active trades found" | Re-run `npm run init-session` |
| Session expired | Re-run `npm run init-session` |
| Login fails | Check BRAVOS_USERNAME and BRAVOS_PASSWORD in .env |
| Scrape returns empty | Check if Bravos site structure changed |
| Missing entry prices | Run detailed scraper: `npx tsx scripts/scrape-bravos-trades.ts` |

## Files

| File | Description |
|------|-------------|
| `scripts/init-session.ts` | Browser authentication |
| `scripts/scrape-active-trades.ts` | Quick scrape of active trades page |
| `scripts/scrape-bravos-trades.ts` | Detailed scrape with all post data |
| `data/sessions/bravos.json` | Saved browser session |
| `data/raw/active-trades-latest.json` | Raw scrape output |
| `data/processed/bravos_trades.json` | Comprehensive trade data |
