# System Overview

Multi-sleeve portfolio automation platform for synchronizing broker holdings with target allocations from various signal sources.

## What This System Does

1. **Ingests target allocations** from signal sources (sleeves)
2. **Fetches current holdings** from brokers
3. **Reconciles** targets vs actuals to generate trade actions
4. **Executes trades** with human-in-the-loop approval

## Core Concepts

### Sleeves (Signal Sources)

A **sleeve** is any source of target portfolio allocations:

| Sleeve Type | Example | How It Works |
|-------------|---------|--------------|
| Research service | Bravos Research | Scrape active trades from website |
| Personal strategy | CSV file | Read allocations from spreadsheet |
| Model portfolio | API | Fetch from external service |

Each sleeve provides:
- **Symbol**: What to hold (e.g., AAPL, EME)
- **Weight**: Relative position size (e.g., 5)
- **Side**: Long or short
- **Optional**: Entry price, stop loss, target price

### Brokers (Execution Targets)

A **broker** is where trades are executed:

| Broker | Status | Features |
|--------|--------|----------|
| Robinhood | Implemented | Fractional shares, commission-free |
| Others | Future | Extensible via adapter pattern |

### Reconciliation

The **reconciliation engine** compares sleeve targets to broker holdings:

```
Sleeve Target: EME weight 5 → $2,500 (at $500/weight)
Broker Holdings: EME $1,800
─────────────────────────────────────────────────────
Action: BUY EME $700
```

## Position Sizing Strategies

### Fixed-Dollar per Weight (Default)

Each weight unit = fixed dollar amount (default: $500)

```
Weight 5 × $500/weight = $2,500 target position
```

**When to use**: Consistent position sizing across all sleeves

### Price-Adjusted

Reduce allocation if price has run up toward target:

```
Entry: $50, Current: $60, Target: $65 (30% upside originally)
Remaining upside: $5 / $15 = 33%
Adjusted allocation: 33% of base
```

**When to use**: Catch-up trades where you're entering late

## Data Flow

```
┌─────────────────┐
│  Sleeve Adapter │  (e.g., Bravos web scraper)
└────────┬────────┘
         │ fetch_targets()
         ▼
┌─────────────────┐
│ Target Allocs   │  data/processed/target_allocations.json
│ (symbol, weight)│
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Reconciliation │  Compare targets vs holdings
│     Engine      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Trade Actions  │  data/processed/reconciliation.json
│ (buy/sell/exit) │
└────────┬────────┘
         │ User approval
         ▼
┌─────────────────┐
│    Execution    │  Broker API calls
│     Engine      │
└─────────────────┘
```

## Safety Features

### ETF Protection

ETFs in the whitelist are never sold during reconciliation:

```python
ETF_SYMBOLS = {"SPY", "QQQ", "VTI", "DBC", ...}
```

If a sleeve includes an ETF, it's reconciled. Non-sleeve ETFs are left alone.

### Human-in-the-Loop

All trades require explicit approval:
- CLI: Interactive confirmation per trade
- Cloud: Telegram bot approval workflow

### Dry-Run Mode

Default mode simulates trades without execution:

```bash
.venv/bin/python -m src.trading.execute_trades --dry-run
```

### Safety Limits

Configurable via environment variables:
- `MAX_TRADE_NOTIONAL`: Max dollars per trade (default: $500)
- `MAX_PORTFOLIO_CHANGE_PCT`: Max portfolio change (default: 5%)
- `MARKET_HOURS_ONLY`: Only trade during market hours

## Deployment Options

### Local (CLI)

Run commands manually:
```bash
npm run scrape-active && npm run reconcile
```

### Cloud (Automated)

- **Google Cloud Run**: Serverless containers
- **Cloud Scheduler**: Periodic reconciliation
- **Telegram Bot**: Mobile approval workflow
- **Gmail Integration**: Signal detection from emails

See [Cloud Deployment](../workflows/cloud-deployment.md) for setup.
