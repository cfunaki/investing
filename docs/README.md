# Multi-Sleeve Portfolio Automation

Automate portfolio allocation across multiple investment sleeves with human-in-the-loop approval.

## Concepts

| Term | Definition |
|------|------------|
| **Sleeve** | A signal source providing target allocations (e.g., Bravos Research, personal strategy) |
| **Broker** | Execution target for trades (e.g., Robinhood) |
| **Reconciliation** | Compare sleeve targets vs broker holdings → generate trade actions |
| **Weight** | Sleeve-defined position size unit (default: $500 per weight) |

## Quick Start (Bravos Sleeve)

```bash
# 1. Setup broker & dependencies
cp .env.example .env  # Fill in credentials
npm install && pip install -r requirements.txt

# 2. Initialize Bravos session
npm run init-session

# 3. Sync portfolio
npm run scrape-active && npm run reconcile

# 4. Review proposed trades in output

# 5. Execute
.venv/bin/python -m src.trading.execute_trades --live
```

Or use the Claude command: `/sync-bravos`

## What do you want to do?

| Goal | Where to go |
|------|-------------|
| Understand the system | [Overview](getting-started/overview.md) |
| Set up from scratch | [Setup Guide](getting-started/setup.md) |
| Add a new sleeve | [Adding a Sleeve](getting-started/adding-a-sleeve.md) |
| Configure Bravos | [Bravos Sleeve](sleeves/bravos.md) |
| Sync portfolio | [Daily Sync Workflow](workflows/daily-sync.md) |
| Execute trades | [Trade Execution](workflows/trade-execution.md) |
| Deploy to cloud | [Cloud Deployment](workflows/cloud-deployment.md) |
| Fix an issue | [Troubleshooting](troubleshoot.md) |
| Look up commands | [CLI Reference](reference/cli-commands.md) |

## Architecture Overview

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Sleeve    │     │  Reconcile  │     │   Broker    │
│  (Bravos)   │────▶│   Engine    │────▶│ (Robinhood) │
└─────────────┘     └─────────────┘     └─────────────┘
       │                   │                   │
       ▼                   ▼                   ▼
  Target Allocs      Trade Actions        Execution
  (weight-based)     (buy/sell/exit)      (fractional)
```

## Directory Structure

```
investing/
├── src/
│   ├── adapters/      # Sleeve adapters (Bravos, etc.)
│   ├── brokers/       # Broker integrations (Robinhood)
│   ├── recon/         # Reconciliation engine
│   ├── trading/       # Trade execution
│   └── ...
├── scripts/           # CLI entry points
├── data/
│   ├── raw/           # Scraped sleeve data
│   ├── processed/     # Targets, holdings, reconciliation
│   ├── sessions/      # Auth tokens
│   └── trades/        # Execution logs
└── docs/              # You are here
```
