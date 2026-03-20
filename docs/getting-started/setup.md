# Setup Guide

Core setup for the portfolio automation platform. This covers broker configuration and dependencies. Sleeve-specific setup is in the [sleeves/](../sleeves/) folder.

## Prerequisites

- **Node.js** 18+ (for Playwright scrapers)
- **Python** 3.11+ (for reconciliation and execution)
- **pip** and **venv** (Python package management)

## 1. Clone and Install Dependencies

```bash
# Clone the repository
git clone <repo-url>
cd investing

# Create Python virtual environment
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install Python dependencies
pip install -r requirements.txt

# Install Node.js dependencies
npm install

# Install Playwright browsers (for web scraping)
npx playwright install chromium
```

## 2. Configure Environment

```bash
# Copy example configuration
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# ===================
# Broker: Robinhood
# ===================
RH_USERNAME=your_robinhood_email
RH_PASSWORD=your_robinhood_password
RH_TOTP_SECRET=                    # Optional: Base32 TOTP secret for 2FA

# ===================
# Safety Controls
# ===================
DRY_RUN=true                       # IMPORTANT: Start with true!
MAX_TRADE_NOTIONAL=500.0           # Max $ per trade
MAX_PORTFOLIO_CHANGE_PCT=0.05      # Max 5% portfolio change

# ===================
# Environment
# ===================
ENVIRONMENT=development
LOG_LEVEL=INFO
```

## 3. Verify Broker Connection

```bash
# Test Robinhood authentication
.venv/bin/python -m src.robinhood.auth
```

Expected output:
```
Logging in as your_email@example.com...
Login successful!
```

If using 2FA without `RH_TOTP_SECRET`, you'll be prompted for the code.

## 4. Verify Data Directories

```bash
# Create required directories
mkdir -p data/{raw,processed,sessions,trades,reports}

# Verify structure
ls -la data/
```

## Next Steps

1. **Configure a sleeve**: Start with [Bravos](../sleeves/bravos.md) or [create your own](adding-a-sleeve.md)
2. **Run your first sync**: Follow [Daily Sync Workflow](../workflows/daily-sync.md)
3. **Optional**: Set up [Cloud Deployment](../workflows/cloud-deployment.md) for automation

## Optional Integrations

These are not required for basic operation but enable advanced features:

### Telegram (Approval Bot)

```env
TELEGRAM_BOT_TOKEN=123456:ABC...   # From @BotFather
TELEGRAM_ALLOWED_USERS=123456789   # Your Telegram user ID
TELEGRAM_CHAT_ID=123456789         # Default notification chat
```

### Gmail (Signal Monitoring)

```env
GMAIL_CREDENTIALS_PATH=data/sessions/gmail_credentials.json
GMAIL_TOKEN_PATH=data/sessions/gmail_token.json
EMAIL_POLL_INTERVAL_SECONDS=180
```

### Supabase (Event Logging)

```env
DATABASE_URL=postgresql://postgres:PASSWORD@db.xxxxx.supabase.co:5432/postgres
```

See [Configuration Reference](../reference/configuration.md) for all options.
