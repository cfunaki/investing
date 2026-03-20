# Trade Execution

Review reconciliation output and execute trades safely.

## Understanding Reconciliation Output

After running reconciliation, review `data/processed/reconciliation.json`:

```json
{
  "deltas": [
    {
      "symbol": "GE",
      "action": "exit",
      "current_value": 3392.42,
      "suggested_trade_value": -3392.42,
      "notes": "Not in Bravos targets - EXIT $3,392"
    },
    {
      "symbol": "NEE",
      "action": "enter",
      "current_value": 0,
      "suggested_trade_value": 2500.0,
      "notes": "Weight 5 → $2,500 (new position)"
    }
  ],
  "summary": {
    "total_sell_value": 16210.00,
    "total_buy_value": 9921.00,
    "net_cash_flow": 6289.00
  }
}
```

### Action Types

| Action | Meaning | Trade Direction |
|--------|---------|-----------------|
| `exit` | Position not in sleeve targets | SELL all shares |
| `sell` | Position overweight | SELL partial (trim) |
| `hold` | Within tolerance | No trade |
| `buy` | Position underweight | BUY more shares |
| `enter` | New position | BUY new position |

### Reading the Notes

Notes explain why each action was generated:

- `"Not in Bravos targets - EXIT $3,392"` → Stock no longer in sleeve
- `"Weight 5 → $2,500 (new position)"` → New sleeve position
- `"Weight 6 → $1,820 (61% of $3,000) (trim $1,180)"` → Price-adjusted trim

## Execution Modes

### Dry-Run (Simulation)

Always start here:

```bash
.venv/bin/python -m src.trading.execute_trades --dry-run
```

Output shows what **would** happen without executing:
```
============================================================
EXECUTING SELLS (9 orders)
============================================================
  [1/9] SELL GE
      Quantity: 11.29 shares
      Value:    $3,392.42
      OK - Order simulated
```

### Live with Confirmation

Interactive mode prompts for each trade:

```bash
.venv/bin/python -m src.trading.execute_trades --live
```

```
WARNING: This will execute REAL trades!
Type 'EXECUTE' to confirm: EXECUTE

  [1/9] SELL GE
      Quantity: 11.29 shares
      Value:    $3,392.42
      Execute? (y/n/q): y
      OK - Order queued
```

Options:
- `y` - Execute this trade
- `n` - Skip this trade
- `q` - Quit (abort remaining trades)

### Live without Confirmation

For non-interactive execution (use with caution):

```bash
.venv/bin/python -m src.trading.execute_trades --live --no-confirm
```

## Execution Order

Trades execute in this order:

1. **EXITS** first (free up cash by closing positions)
2. **SELLS** second (trim positions to target)
3. **BUYS** third (add to existing positions)
4. **ENTERS** last (open new positions)

This ensures cash is available for buys.

## Handling Failures

### Rate Limiting (429 Error)

Robinhood may rate-limit API requests:

```
Error in request_post: Received 429
```

**Solution:**
1. Wait 60 seconds
2. Check pending orders: `--pending`
3. Re-run execution (already-executed orders will be skipped or show "not enough shares")

### Fractional Sell Failures

Some sells may fail with "API returned None":

```
ERROR: {'status': 'error', 'error': 'API returned None', 'symbol': 'AA'}
```

**Solution:** The system automatically retries with dollar-based sells instead of quantity-based.

### Symbol Not Found

For symbols not available on Robinhood:

```
400 Client Error: Bad Request for url: https://api.robinhood.com/quotes/?symbols=ALUM
```

**Solution:** Skip this trade. Find a US-listed alternative or use a different broker.

### Market Closed

Orders placed outside market hours are queued for next open.

## Verifying Execution

### Check Pending Orders

```bash
.venv/bin/python -m src.trading.execute_trades --pending
```

```
Pending Orders (12):
  BUY ? x26.22 [queued]
  SELL ? x11.29 [queued]
  ...
```

### View Trade Logs

Executed trades are logged to `data/trades/trades-YYYYMMDD.json`:

```bash
cat data/trades/trades-$(date +%Y%m%d).json | jq '.[] | {type, symbol, state}'
```

### Re-fetch Holdings

After orders fill, refresh holdings:

```bash
.venv/bin/python -m src.robinhood.holdings
```

### Re-run Reconciliation

Verify portfolio is aligned:

```bash
.venv/bin/python -m src.recon.reconcile --fixed-dollar 500
```

Expected: Minimal or no deltas (within $50 tolerance).

## Safety Checklist

Before executing live trades:

- [ ] Reviewed dry-run output
- [ ] Net cash flow is reasonable
- [ ] No unexpected large exits
- [ ] ETFs are not being sold (unless in sleeve)
- [ ] Market is open (or queued orders are intentional)
- [ ] `DRY_RUN=false` in .env (or using `--live` flag)

## Rollback

If trades were executed incorrectly:

1. **Cancel pending orders** via Robinhood app/website
2. **Manually reverse** filled orders if needed
3. **Fix the sleeve data** or reconciliation logic
4. **Re-run** with corrected data
