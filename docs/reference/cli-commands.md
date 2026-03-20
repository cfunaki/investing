# CLI Commands Reference

All available commands for the portfolio automation platform.

## NPM Scripts

Run with `npm run <script>`:

| Command | Description | Output |
|---------|-------------|--------|
| `init-session` | Authenticate with Bravos (browser-based) | `data/sessions/bravos.json` |
| `scrape` | Scrape Bravos /ideas/ page (historical) | `data/raw/ideas-*.json` |
| `scrape-active` | Scrape Bravos /research/ page (current trades) | `data/raw/active-trades-*.json` |
| `reconcile` | Run full pipeline: normalize → derive → reconcile | `data/processed/*.json` |
| `report` | Generate markdown report | `data/reports/*.md` |

## Python Modules

Run with `.venv/bin/python -m <module>`:

### Authentication

```bash
# Test Robinhood login
.venv/bin/python -m src.robinhood.auth
```

### Data Pipeline

```bash
# Normalize scraped ideas
.venv/bin/python -m src.parsing.normalize_ideas

# Derive target allocations from normalized ideas
.venv/bin/python -m src.parsing.derive_positions

# Fetch current Robinhood holdings
.venv/bin/python -m src.robinhood.holdings
```

### Reconciliation

```bash
# Fixed-dollar reconciliation (recommended)
.venv/bin/python -m src.recon.reconcile --fixed-dollar 500

# Price-adjusted reconciliation (for catch-up trades)
.venv/bin/python -m src.recon.reconcile --price-adjusted 500 0.30

# Stocks-only (excludes ETFs from target pool)
.venv/bin/python -m src.recon.reconcile --stocks-only

# Default percentage-based (not recommended)
.venv/bin/python -m src.recon.reconcile
```

**Reconciliation Flags:**

| Flag | Arguments | Description |
|------|-----------|-------------|
| `--fixed-dollar` | `<dpw>` | Use fixed dollar-per-weight (e.g., 500) |
| `--price-adjusted` | `<dpw> <target_pct>` | Fixed-dollar with price adjustment |
| `--stocks-only` | - | Exclude ETFs from reconciliation pool |

### Trade Execution

```bash
# Simulate trades (no execution)
.venv/bin/python -m src.trading.execute_trades --dry-run

# Execute real trades (with per-trade confirmation)
.venv/bin/python -m src.trading.execute_trades --live

# Execute without prompts (non-interactive)
.venv/bin/python -m src.trading.execute_trades --live --no-confirm

# Show pending orders
.venv/bin/python -m src.trading.execute_trades --pending
```

**Execution Flags:**

| Flag | Description |
|------|-------------|
| `--dry-run` | Simulate without executing |
| `--live` | Execute real trades |
| `--no-confirm` | Skip per-trade confirmation (use with `--live`) |
| `--pending` | Show pending orders only |

### Reporting

```bash
# Generate markdown report
.venv/bin/python -m src.reporting.markdown_report
```

## TypeScript Scripts

Run with `npx tsx scripts/<script>.ts`:

```bash
# Initialize Bravos session
npx tsx scripts/init-session.ts

# Scrape active trades (quick)
npx tsx scripts/scrape-active-trades.ts

# Scrape historical ideas
npx tsx scripts/scrape-ideas.ts

# Scrape comprehensive trade data (slower, more detail)
npx tsx scripts/scrape-bravos-trades.ts
```

## Claude Commands

Invoke with `/<command>`:

| Command | Description |
|---------|-------------|
| `/sync-bravos` | Full Bravos sync workflow |

## Makefile Targets

Run with `make <target>`:

```bash
make init       # First-time Bravos login
make scrape     # Scrape active trades
make normalize  # Parse and derive positions
make reconcile  # Fetch holdings + reconcile
make report     # Generate reports
make all        # Full pipeline
make clean      # Delete generated data
```

## Common Workflows

### Full Bravos Sync (Manual)

```bash
npm run scrape-active
.venv/bin/python -m src.parsing.normalize_ideas
.venv/bin/python -m src.parsing.derive_positions
.venv/bin/python -m src.robinhood.holdings
.venv/bin/python -m src.recon.reconcile --fixed-dollar 500
.venv/bin/python -m src.trading.execute_trades --dry-run
# Review output, then:
.venv/bin/python -m src.trading.execute_trades --live
```

### Quick Reconciliation Check

```bash
npm run reconcile
cat data/processed/reconciliation.json | jq '.summary'
```

### Debug Authentication

```bash
# Robinhood
.venv/bin/python -m src.robinhood.auth

# Bravos (re-authenticate)
npm run init-session
```
