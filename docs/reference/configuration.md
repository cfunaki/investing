# Configuration Reference

All environment variables used by the platform.

## Quick Setup

```bash
cp .env.example .env
# Edit .env with your values
```

## Core Configuration

### Broker: Robinhood

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `RH_USERNAME` | Yes | Robinhood account email | `user@example.com` |
| `RH_PASSWORD` | Yes | Robinhood password | `secretpassword` |
| `RH_TOTP_SECRET` | No | Base32 TOTP secret for 2FA | `JBSWY3DPEHPK3PXP` |

**Getting TOTP Secret:**
When setting up 2FA in Robinhood, you can usually view the secret key. It's the base32 string used to generate time-based codes.

### Sleeve: Bravos

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `BRAVOS_BASE_URL` | No | Bravos website URL | `https://bravosresearch.com` |
| `BRAVOS_USERNAME` | Yes* | Bravos account email | `user@example.com` |
| `BRAVOS_PASSWORD` | Yes* | Bravos password | `secretpassword` |

*Required only if using Bravos sleeve

## Safety Controls

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DRY_RUN` | No | `true` | If true, simulate trades without executing |
| `MAX_TRADE_NOTIONAL` | No | `500.0` | Maximum dollars per single trade |
| `MAX_PORTFOLIO_CHANGE_PCT` | No | `0.05` | Maximum portfolio change (5%) |
| `MARKET_HOURS_ONLY` | No | `true` | Only trade during market hours |

**Important:** Always start with `DRY_RUN=true` until you're confident the system works correctly.

## Optional Integrations

### Telegram (Approval Bot)

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes* | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | Yes* | Comma-separated user IDs |
| `TELEGRAM_CHAT_ID` | No | Default chat for notifications |

*Required only if using Telegram approval workflow

**Setup:**
1. Message @BotFather on Telegram
2. Send `/newbot` and follow prompts
3. Copy the bot token
4. Get your user ID from @userinfobot

### Gmail (Signal Monitoring)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GMAIL_CREDENTIALS_PATH` | Yes* | `data/sessions/gmail_credentials.json` | OAuth client credentials |
| `GMAIL_TOKEN_PATH` | Yes* | `data/sessions/gmail_token.json` | OAuth access token |
| `EMAIL_POLL_INTERVAL_SECONDS` | No | `180` | How often to check for emails |

*Required only if using email signal monitoring

**Setup:**
1. Create OAuth credentials in Google Cloud Console
2. Download JSON and save to `GMAIL_CREDENTIALS_PATH`
3. First run will prompt for authorization

### Database (Supabase)

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes* | PostgreSQL connection string |

*Required only if using cloud deployment with event logging

**Format:**
```
postgresql://postgres:PASSWORD@db.xxxxx.supabase.co:5432/postgres
```

### Browser Worker (Cloud)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BROWSER_WORKER_URL` | No | `http://localhost:8001` | Browser worker service URL |
| `BROWSER_WORKER_TIMEOUT` | No | `120` | Request timeout in seconds |

For cloud deployment, set to your Cloud Run browser worker URL:
```
BROWSER_WORKER_URL=https://investing-browser-worker-xxxxx.run.app
```

## Application Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ENVIRONMENT` | No | `development` | Environment name |
| `LOG_LEVEL` | No | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `APPROVAL_EXPIRY_MINUTES` | No | `10` | How long approval requests are valid |

## Example .env Files

### Minimal (Local, Bravos Only)

```env
# Broker
RH_USERNAME=user@example.com
RH_PASSWORD=mypassword

# Sleeve
BRAVOS_USERNAME=user@example.com
BRAVOS_PASSWORD=mypassword

# Safety
DRY_RUN=true
```

### Full (Cloud Deployment)

```env
# Database
DATABASE_URL=postgresql://postgres:xxx@db.xxx.supabase.co:5432/postgres

# Broker
RH_USERNAME=user@example.com
RH_PASSWORD=mypassword
RH_TOTP_SECRET=JBSWY3DPEHPK3PXP

# Sleeve
BRAVOS_BASE_URL=https://bravosresearch.com
BRAVOS_USERNAME=user@example.com
BRAVOS_PASSWORD=mypassword

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_ALLOWED_USERS=123456789
TELEGRAM_CHAT_ID=123456789

# Gmail
GMAIL_CREDENTIALS_PATH=data/sessions/gmail_credentials.json
GMAIL_TOKEN_PATH=data/sessions/gmail_token.json
EMAIL_POLL_INTERVAL_SECONDS=180

# Browser Worker
BROWSER_WORKER_URL=https://investing-browser-worker-xxx.run.app
BROWSER_WORKER_TIMEOUT=120

# Safety
DRY_RUN=false
MAX_TRADE_NOTIONAL=500.0
MAX_PORTFOLIO_CHANGE_PCT=0.05
MARKET_HOURS_ONLY=true

# Application
ENVIRONMENT=production
LOG_LEVEL=INFO
APPROVAL_EXPIRY_MINUTES=10
```

## Security Notes

1. **Never commit .env to git** - It contains secrets
2. **Use Secret Manager in production** - Google Cloud Secret Manager or similar
3. **Rotate credentials periodically** - Especially after any suspected exposure
4. **Limit Telegram allowed users** - Only your user ID
