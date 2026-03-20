# Cloud Deployment

Deploy the portfolio automation platform to Google Cloud for fully automated operation.

**Prerequisites:** Complete [local setup](../getting-started/setup.md) first.

## What You'll Set Up

- **Cloud Run**: Serverless containers for orchestrator and browser worker
- **Cloud Scheduler**: Automated jobs for email polling, reconciliation, approval expiry
- **Secret Manager**: Secure credential storage
- **Telegram Webhook**: Mobile approval workflow
- **Supabase**: PostgreSQL database for event logging

---

## Prerequisites

- Google Cloud account with billing enabled
- Supabase account (free tier works)
- Telegram account
- Gmail account (for receiving Bravos notifications)
- Robinhood account

## Phase 1: Infrastructure Setup

### 1.1 Create Supabase Project

1. Go to [supabase.com](https://supabase.com) and create a new project
2. Note down:
   - Project URL: `https://xxxxx.supabase.co`
   - Database password (set during creation)
   - Connection string: Find in Settings > Database > Connection string > URI

3. Run the schema migration:
   - Go to SQL Editor in Supabase dashboard
   - Copy contents of `src/db/schema.sql`
   - Run the SQL

4. Verify tables were created:
   ```sql
   SELECT table_name FROM information_schema.tables
   WHERE table_schema = 'public';
   ```

### 1.2 Set Up Google Cloud Project

```bash
# Set your project ID
export PROJECT_ID="your-project-id"

# Create project (or use existing)
gcloud projects create $PROJECT_ID --name="Investing Automation"
gcloud config set project $PROJECT_ID

# Enable required APIs
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    cloudscheduler.googleapis.com

# Create Artifact Registry repository
gcloud artifacts repositories create investing \
    --repository-format=docker \
    --location=us-central1 \
    --description="Docker images for investing automation"
```

### 1.3 Create Secrets in Secret Manager

```bash
# Database URL (from Supabase)
echo -n "postgresql://postgres:YOUR_PASSWORD@db.xxxxx.supabase.co:5432/postgres" | \
    gcloud secrets create database-url --data-file=-

# Robinhood credentials
echo -n "your-robinhood-email" | \
    gcloud secrets create rh-username --data-file=-
echo -n "your-robinhood-password" | \
    gcloud secrets create rh-password --data-file=-

# Bravos credentials
echo -n "your-bravos-email" | \
    gcloud secrets create bravos-username --data-file=-
echo -n "your-bravos-password" | \
    gcloud secrets create bravos-password --data-file=-

# Telegram bot token (create bot first - see below)
echo -n "your-telegram-bot-token" | \
    gcloud secrets create telegram-bot-token --data-file=-
```

### 1.4 Create Telegram Bot

1. Open Telegram and search for [@BotFather](https://t.me/botfather)
2. Send `/newbot` and follow prompts
3. Note the bot token (looks like `123456:ABC-DEF1234...`)
4. Get your Telegram user ID:
   - Send a message to [@userinfobot](https://t.me/userinfobot)
   - Note the ID number

### 1.5 Grant Cloud Run Access to Secrets

```bash
# Get the Cloud Run service account
export SA_EMAIL="$(gcloud iam service-accounts list \
    --filter='displayName:Compute Engine default service account' \
    --format='value(email)')"

# Grant access to each secret
for secret in database-url rh-username rh-password bravos-username bravos-password telegram-bot-token; do
    gcloud secrets add-iam-policy-binding $secret \
        --member="serviceAccount:$SA_EMAIL" \
        --role="roles/secretmanager.secretAccessor"
done
```

## Phase 2: Local Development Setup

### 2.1 Create Local Environment File

```bash
# Copy example and fill in values
cp .env.example .env
```

Edit `.env`:
```env
# Database
DATABASE_URL=postgresql://postgres:PASSWORD@db.xxxxx.supabase.co:5432/postgres

# Robinhood
RH_USERNAME=your-robinhood-email
RH_PASSWORD=your-robinhood-password
RH_TOTP_SECRET=your-totp-secret-if-using-2fa

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_ALLOWED_USERS=123456789
TELEGRAM_CHAT_ID=123456789

# Bravos
BRAVOS_BASE_URL=https://bravosresearch.com
BRAVOS_USERNAME=your-bravos-email
BRAVOS_PASSWORD=your-bravos-password

# Browser Worker (local)
BROWSER_WORKER_URL=http://localhost:8001

# Safety
DRY_RUN=true
MAX_TRADE_NOTIONAL=100.0
ENVIRONMENT=development
```

### 2.2 Install Dependencies

```bash
# Python dependencies
pip install -r requirements.txt

# Node.js dependencies (for Playwright scraper)
npm install

# Install Playwright browsers
npx playwright install chromium
```

### 2.3 Run Locally

Terminal 1 - Browser Worker:
```bash
cd browser-worker
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

Terminal 2 - Orchestrator:
```bash
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

### 2.4 Test Health Endpoints

```bash
# Orchestrator
curl http://localhost:8000/health

# Browser Worker
curl http://localhost:8001/health
```

## Phase 3: Deploy to Cloud Run

### 3.1 Build and Deploy

```bash
# Deploy both services
gcloud builds submit --config cloudbuild.yaml
```

### 3.2 Get Service URLs

```bash
# Get orchestrator URL
gcloud run services describe investing-orchestrator \
    --region=us-central1 --format='value(status.url)'

# Get browser worker URL
gcloud run services describe investing-browser-worker \
    --region=us-central1 --format='value(status.url)'
```

### 3.3 Test Deployed Services

```bash
# Replace with your actual URLs
curl https://investing-orchestrator-xxxxx.run.app/health
curl https://investing-browser-worker-xxxxx.run.app/health
```

## Phase 4: Set Up Cloud Scheduler

### 4.1 Create Email Polling Job

```bash
# Poll for emails every 3 minutes
gcloud scheduler jobs create http poll-email \
    --location=us-central1 \
    --schedule="*/3 * * * *" \
    --uri="https://investing-orchestrator-xxxxx.run.app/jobs/poll-email" \
    --http-method=POST \
    --oidc-service-account-email="$SA_EMAIL"
```

### 4.2 Create Reconciliation Job

```bash
# Run reconciliation every 30 minutes as safety net
gcloud scheduler jobs create http reconcile \
    --location=us-central1 \
    --schedule="*/30 * * * *" \
    --uri="https://investing-orchestrator-xxxxx.run.app/jobs/reconcile" \
    --http-method=POST \
    --oidc-service-account-email="$SA_EMAIL"
```

### 4.3 Create Approval Expiration Job

```bash
# Expire old approvals every 5 minutes
gcloud scheduler jobs create http expire-approvals \
    --location=us-central1 \
    --schedule="*/5 * * * *" \
    --uri="https://investing-orchestrator-xxxxx.run.app/jobs/expire-approvals" \
    --http-method=POST \
    --oidc-service-account-email="$SA_EMAIL"
```

## Phase 5: Set Up Telegram Webhook

### 5.1 Register Webhook

```bash
# Set webhook to receive Telegram updates
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://investing-orchestrator-xxxxx.run.app/webhooks/telegram"}'
```

### 5.2 Test Bot

1. Open Telegram and search for your bot
2. Send `/start`
3. Check Cloud Run logs for the incoming message

## Verification Checklist

- [ ] Supabase: Tables created, can connect from Cloud Run
- [ ] Cloud Run: Both services deployed and healthy
- [ ] Secrets: All secrets accessible by services
- [ ] Cloud Scheduler: Jobs created and triggering
- [ ] Telegram: Bot responding to messages

## Troubleshooting

### Database Connection Issues

```bash
# Check logs
gcloud run services logs read investing-orchestrator --region=us-central1

# Verify secret value
gcloud secrets versions access latest --secret=database-url
```

### Browser Worker Timeout

The browser worker has a 5-minute timeout. If scrapes are failing:

1. Check if Bravos credentials are correct
2. Check if Bravos site structure has changed
3. Look at browser worker logs for Playwright errors

### Telegram Not Receiving Messages

1. Verify webhook is set:
   ```bash
   curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
   ```
2. Check that webhook URL is correct
3. Verify TELEGRAM_ALLOWED_USERS includes your user ID

## Cost Monitoring

Monitor your costs in Google Cloud Console:
- Cloud Run: Should be minimal with scale-to-zero
- Cloud Scheduler: ~$0.10/month for 3 jobs
- Secret Manager: Free tier should cover usage
- Artifact Registry: Minimal storage costs

Expected total: **~$2-5/month** with cold starts accepted
