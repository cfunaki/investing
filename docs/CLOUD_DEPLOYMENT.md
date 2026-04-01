# Cloud Deployment Checklist

One-time setup guide for deploying the investing automation platform to GCP.

## Prerequisites

- [ ] GCP project created (`investing-automation-490206`)
- [ ] `gcloud` CLI installed and authenticated
- [ ] Project ID set: `gcloud config set project investing-automation-490206`

## Step 1: Enable APIs

```bash
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com
```

## Step 2: Create Artifact Registry Repository

```bash
gcloud artifacts repositories create investing \
  --repository-format=docker \
  --location=us-central1 \
  --description="Investing automation container images"
```

## Step 3: Create Secrets in Secret Manager

Create each secret (replace values with actual credentials):

```bash
# Database
echo -n "postgresql://user:pass@host:5432/db" | \
  gcloud secrets create DATABASE_URL --data-file=-

# Robinhood
echo -n "your_rh_username" | \
  gcloud secrets create RH_USERNAME --data-file=-
echo -n "your_rh_password" | \
  gcloud secrets create RH_PASSWORD --data-file=-

# Telegram
echo -n "your_bot_token" | \
  gcloud secrets create TELEGRAM_BOT_TOKEN --data-file=-
echo -n "your_chat_id" | \
  gcloud secrets create TELEGRAM_CHAT_ID --data-file=-
echo -n "your_user_id" | \
  gcloud secrets create TELEGRAM_ALLOWED_USERS --data-file=-

# Bravos (for browser-worker)
echo -n "your_bravos_username" | \
  gcloud secrets create BRAVOS_USERNAME --data-file=-
echo -n "your_bravos_password" | \
  gcloud secrets create BRAVOS_PASSWORD --data-file=-

# Gmail (if using Gmail API)
# Note: Gmail OAuth tokens may need special handling
```

**Verify secrets exist:**
```bash
gcloud secrets list
```

## Step 4: Create Service Account

```bash
# Create service account
gcloud iam service-accounts create investing-orchestrator \
  --display-name="Investing Orchestrator Service"

# Get the email
SA_EMAIL="investing-orchestrator@investing-automation-490206.iam.gserviceaccount.com"

# Grant permissions
gcloud projects add-iam-policy-binding investing-automation-490206 \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding investing-automation-490206 \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker"
```

## Step 5: Deploy to Cloud Run

```bash
gcloud builds submit --config cloudbuild.yaml
```

This builds and deploys both services:
- `investing-orchestrator` - Main API
- `investing-browser-worker` - Playwright scraper

**Get the deployed URL:**
```bash
gcloud run services describe investing-orchestrator \
  --region=us-central1 \
  --format='value(status.url)'
```

Save this URL - you'll need it for the next steps.

## Step 6: Register Telegram Webhook

Replace `YOUR_BOT_TOKEN` and `ORCHESTRATOR_URL`:

```bash
TELEGRAM_BOT_TOKEN="your_bot_token_here"
ORCHESTRATOR_URL="https://investing-orchestrator-xxxxx.us-central1.run.app"

curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${ORCHESTRATOR_URL}/webhooks/telegram"
```

**Verify webhook:**
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

Should show your URL and no errors.

## Step 7: Create Cloud Scheduler Jobs

Replace `ORCHESTRATOR_URL` with your actual URL:

```bash
ORCHESTRATOR_URL="https://investing-orchestrator-xxxxx.us-central1.run.app"
SA_EMAIL="investing-orchestrator@investing-automation-490206.iam.gserviceaccount.com"

# Bravos email check - every 3 hours
gcloud scheduler jobs create http bravos-email-check \
  --location=us-central1 \
  --schedule="0 */3 * * *" \
  --uri="${ORCHESTRATOR_URL}/jobs/poll-email" \
  --http-method=POST \
  --oidc-service-account-email="${SA_EMAIL}"

# Buffett 13F check - twice daily (6am and 6pm UTC)
gcloud scheduler jobs create http buffett-13f-check \
  --location=us-central1 \
  --schedule="0 6,18 * * *" \
  --uri="${ORCHESTRATOR_URL}/jobs/poll-buffett" \
  --http-method=POST \
  --oidc-service-account-email="${SA_EMAIL}"

# Expire stale approvals - every 5 minutes
gcloud scheduler jobs create http expire-approvals \
  --location=us-central1 \
  --schedule="*/5 * * * *" \
  --uri="${ORCHESTRATOR_URL}/jobs/expire-approvals" \
  --http-method=POST \
  --oidc-service-account-email="${SA_EMAIL}"
```

**Verify jobs:**
```bash
gcloud scheduler jobs list --location=us-central1
```

## Step 8: Verify Deployment

### Test health endpoint
```bash
curl "${ORCHESTRATOR_URL}/health"
```

### Test Telegram commands
Send these to your bot:
- `/start` - Should get welcome message
- `/status` - Should show system status
- `/holdings` - Should show portfolio
- `/portfolio` - Should show sleeve breakdown

### Manually trigger a job
```bash
gcloud scheduler jobs run bravos-email-check --location=us-central1
```

Check Cloud Run logs:
```bash
gcloud run services logs read investing-orchestrator --region=us-central1 --limit=50
```

---

## Ongoing Deployments

After initial setup, deploying code changes is just:

```bash
gcloud builds submit --config cloudbuild.yaml
```

The webhook and scheduler jobs persist - no need to recreate them.

---

## Troubleshooting

### Telegram webhook not working
```bash
# Check webhook status
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"

# Check for pending updates (shouldn't be any with webhook)
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```

### Cloud Run not starting
```bash
# Check logs
gcloud run services logs read investing-orchestrator --region=us-central1

# Check if secrets are accessible
gcloud run services describe investing-orchestrator --region=us-central1
```

### Scheduler jobs failing
```bash
# Check job status
gcloud scheduler jobs describe bravos-email-check --location=us-central1

# Check recent executions
gcloud logging read "resource.type=cloud_scheduler_job" --limit=10
```

---

## Cost Estimate

With min-instances=0 (scale to zero):
- **Cloud Run**: ~$0-5/month (pay per request)
- **Cloud Scheduler**: ~$0.10/month per job
- **Secret Manager**: ~$0.06/month per secret
- **Artifact Registry**: ~$0.10/GB storage

**Total**: < $10/month for light usage
