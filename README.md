# Zoho Sprints MCP -> Google Sheets (verification-first)

This is an initial, read-only connector scaffold. `MODE=discover` connects and enumerates tools but never calls a Zoho tool or writes Sheets. `MODE=sync` uses a specifically selected read tool, and writes the returned JSON into a Google Sheet tab **only** if `GOOGLE_SPREADSHEET_ID` is supplied.

## Security note
The Zoho MCP URL includes a credential. **Rotate the URL shared in chat before deploying**, restrict the Zoho MCP server to necessary read actions, never log the URL and never commit it to Git. The application does not use any supplied key or write to any external account during package creation.

## 1. macOS local connection test

```bash
cd zoho-sprints-sheets-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MODE=discover
read -r -s -p 'Paste newly rotated Zoho MCP URL: ' ZOHO_MCP_URL; echo
export ZOHO_MCP_URL
python -m src.main
unset ZOHO_MCP_URL
```

Expected: `MCP handshake succeeded; N tools visible`; tool names and required argument names will be printed. If it fails, confirm MCP API access, server enabled, outbound HTTPS access, and regenerated URL validity. Never paste the complete endpoint in logs or support tickets.

Do not select a tool until its semantics and arguments have been reviewed. Tool-name checks are **defense in depth, not an authorization boundary**; configure read-only actions on the Zoho MCP server itself.

## 2. GCP preparation (commands to run after choosing the project)

Set variables for your own Google Cloud project and spreadsheet:

```bash
export PROJECT_ID='YOUR_GCP_PROJECT'
export RUN_REGION='africa-south1'
export SCHEDULER_REGION='europe-west1'
export JOB='zoho-sprints-sheets-sync'
export SA_NAME='zoho-sprints-sync'
gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com sheets.googleapis.com cloudscheduler.googleapis.com
# Create artifact repo, service identity, and secret (if not already existing).
gcloud artifacts repositories create integration-images --repository-format=docker --location="$RUN_REGION"
gcloud iam service-accounts create "$SA_NAME" --display-name='Zoho Sprints Sheets Sync'
export RUN_SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
# Interactive input is kept out of shell history. Do not paste the key into a command string.
read -r -s -p 'Paste NEW Zoho MCP URL: ' ZOHO_MCP_URL; echo
printf '%s' "$ZOHO_MCP_URL" | gcloud secrets create zoho-sprints-mcp-url --data-file=-
unset ZOHO_MCP_URL
gcloud secrets add-iam-policy-binding zoho-sprints-mcp-url --member="serviceAccount:$RUN_SA" --role='roles/secretmanager.secretAccessor'
```

The commands assume appropriate IAM access and that resources do not already exist. If they exist, inspect and reuse them rather than blindly rerunning creation commands.

## 3. Build and deploy discovery-only Cloud Run Job

```bash
export IMAGE="$RUN_REGION-docker.pkg.dev/$PROJECT_ID/integration-images/$JOB:initial"
gcloud builds submit --tag "$IMAGE" .
gcloud run jobs deploy "$JOB" --image="$IMAGE" --region="$RUN_REGION" \
  --service-account="$RUN_SA" --set-env-vars='MODE=discover' \
  --set-secrets='ZOHO_MCP_URL=zoho-sprints-mcp-url:1' --max-retries=0 --task-timeout=10m
gcloud run jobs execute "$JOB" --region="$RUN_REGION" --wait
```

Review Cloud Run Job logs. The tool list will determine the next step. No production Google Sheet is modified in this mode.

## 4. Optional Sheet connection test AFTER tool verification

Create a blank Google Sheet with tab `Raw_MCP_Export` and share it with the **Cloud Run runtime service account** (`RUN_SA`) as Editor. The Google Sheets API uses Cloud Run's application-default credentials (no service-account JSON key). Set `MCP_READ_TOOL` and `MCP_TOOL_ARGS_JSON` using real discovered tool schemas. First set no `GOOGLE_SPREADSHEET_ID`: this performs a read but does not write. Then add the Sheet ID when ready.

The initial data format is deliberately raw (one JSON string per returned record). After inspecting real MCP schemas, we should build normalised tabs for Projects, Sprints, Work_Items, Bugs, Daily_Snapshots and Sync_Log. Full historical burn-down needs daily snapshots; this prototype does not manufacture historical states.

## 5. Scheduler (AFTER read and Sheet tests pass)

Cloud Scheduler should call `https://run.googleapis.com/v2/projects/PROJECT_ID/locations/RUN_REGION/jobs/JOB:run` using an **OAuth access token**, not OIDC. Its service account needs `roles/run.invoker` on the job. Use the console Cloud Run Job -> Triggers -> Add Scheduler Trigger, with timezone `Africa/Johannesburg` and desired cadence. Scheduler location can be `europe-west1` while the Cloud Run job is `africa-south1`. Do not schedule until the sync is confirmed idempotent.

## Pending before production
- Validate that MCP tools expose all required read APIs and response pagination.
- Handle cursor pagination and stable unique identifiers for merge/upsert.
- Build structured Sheets, historical daily snapshots, and separate GitHub data feed.
- Add retry/backoff and data-quality checks, plus access review and alerts.
- Avoid storing PII or confidential task descriptions unnecessarily.
