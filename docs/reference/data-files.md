# Data Files Reference

Structure and contents of the `data/` directory.

## Directory Structure

```
data/
├── raw/                    # Raw data from sleeves
├── processed/              # Normalized and computed data
├── sessions/               # Authentication tokens
├── trades/                 # Execution logs
└── reports/                # Generated reports
```

## Raw Data (`data/raw/`)

Unprocessed data directly from sleeves.

| File | Source | Description |
|------|--------|-------------|
| `active-trades-latest.json` | Bravos | Current active trades from /research/ |
| `active-trades-YYYYMMDD-HHMMSS.json` | Bravos | Timestamped snapshots |
| `ideas-latest.json` | Bravos | Historical ideas from /ideas/ |

### Example: `active-trades-latest.json`

```json
[
  {
    "symbol": "EME",
    "companyName": "EMCOR Group",
    "currentWeight": 3,
    "side": "long",
    "status": "active"
  }
]
```

## Processed Data (`data/processed/`)

Normalized and computed data ready for reconciliation.

| File | Description |
|------|-------------|
| `ideas.json` | Normalized ideas with cleaned prices/dates |
| `target_allocations.json` | Derived target positions |
| `robinhood_holdings.json` | Current broker holdings |
| `reconciliation.json` | Reconciliation results (deltas) |
| `bravos_trades.json` | Comprehensive trade data with posts |

### `target_allocations.json`

```json
{
  "allocations": [
    {
      "symbol": "EME",
      "weight": 3,
      "side": "long",
      "target_pct": 0.0484
    }
  ],
  "total_weight": 62,
  "generated_at": "2026-03-18T10:30:00"
}
```

### `robinhood_holdings.json`

```json
{
  "holdings": [
    {
      "symbol": "EME",
      "quantity": 1.5,
      "market_value": 1065.00,
      "current_price": 710.00,
      "current_pct": 0.01
    }
  ],
  "total_value": 106889.15,
  "account": {
    "portfolio_value": 106889.15,
    "cash": 5234.50
  },
  "generated_at": "2026-03-18T10:30:00"
}
```

### `reconciliation.json`

```json
{
  "deltas": [
    {
      "symbol": "EME",
      "action": "buy",
      "current_value": 1065.00,
      "suggested_trade_value": 435.00,
      "notes": "Weight 3 → $1,500 (add $435)"
    }
  ],
  "summary": {
    "dollars_per_weight": 500,
    "total_weight": 62,
    "total_sell_value": 16210.00,
    "total_buy_value": 9921.00,
    "net_cash_flow": 6289.00
  },
  "generated_at": "2026-03-18T10:30:00"
}
```

### `bravos_trades.json`

Comprehensive trade data with all posts:

```json
{
  "trades": {
    "EME": {
      "symbol": "EME",
      "companyName": "EMCOR Group",
      "status": "active",
      "side": "long",
      "entryDate": "2026-01-15",
      "entryPrice": 687.73,
      "currentStop": 705,
      "currentWeight": 3,
      "posts": [
        {
          "date": "2026-01-15",
          "action": "enter",
          "price": 687.73,
          "weight": 5,
          "stop": 635
        },
        {
          "date": "2026-02-09",
          "action": "trim",
          "price": 782.24,
          "weight": 4,
          "stop": 685
        }
      ]
    }
  },
  "lastUpdated": "2026-03-14T05:31:18"
}
```

## Sessions (`data/sessions/`)

Authentication tokens and cookies.

| File | Description | Refresh |
|------|-------------|---------|
| `bravos.json` | Bravos browser cookies | `npm run init-session` |
| `gmail_credentials.json` | Gmail OAuth client credentials | Manual (from Google Cloud) |
| `gmail_token.json` | Gmail OAuth access token | Auto-refreshed |

### Security Note

**Never commit session files to git.** They contain authentication secrets.

The `.gitignore` should include:
```
data/sessions/
```

## Trade Logs (`data/trades/`)

Execution history and pending orders.

| File | Description |
|------|-------------|
| `trades-YYYYMMDD.json` | Executed trades for date |
| `pending-sells-*.json` | Pending sell orders (deprecated) |

### `trades-20260318.json`

```json
[
  {
    "type": "sell",
    "symbol": "GE",
    "quantity": 11.29,
    "state": "queued",
    "logged_at": "2026-03-18T14:35:00"
  },
  {
    "type": "buy",
    "symbol": "NEE",
    "intended_amount": 2500.00,
    "state": "queued",
    "logged_at": "2026-03-18T14:35:05"
  }
]
```

## Reports (`data/reports/`)

Generated analysis reports.

| File | Description |
|------|-------------|
| `reconciliation-latest.md` | Latest reconciliation summary |
| `reconciliation-YYYYMMDD.md` | Dated snapshots |
| `*.csv` | CSV exports for spreadsheets |

## Cleanup

To reset all generated data:

```bash
# Remove all processed data
rm -rf data/processed/*

# Remove all raw data
rm -rf data/raw/*

# Remove trade logs
rm -rf data/trades/*

# Keep sessions (to avoid re-authentication)
# rm -rf data/sessions/*  # Only if needed
```

Or use the Makefile:
```bash
make clean
```
