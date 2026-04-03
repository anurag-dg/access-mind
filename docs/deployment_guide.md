# Access Mind — GCP Deployment Guide

**Project:** `cerberus-491806`  
**Region:** `us-central1`  
**Service account:** `cerberus-sa@cerberus-491806.iam.gserviceaccount.com`

---

## How it works on Cloud Run

```
Internet → Cloud Run (access-mind) → GCP APIs
                  │                  (IAM, Compute, Recommender)
                  │
                  ├── GEMINI_API_KEY  (from Secret Manager)
                  └── GCP auth        (SA attached to service, no JSON key needed)
```

The SA is **attached** to the Cloud Run service, so it authenticates to GCP via ADC automatically — no `GCP_SERVICE_ACCOUNT_JSON` env var is needed in production.

---

## Step 1 — Prerequisites (one-time, local machine)

### Install gcloud CLI
```bash
# Windows: download installer from
# https://cloud.google.com/sdk/docs/install

# Then authenticate
gcloud auth login        # opens browser → sign in as anuraggcp101@gmail.com
gcloud auth application-default login
```

### Verify
```bash
gcloud config get-value account
# should print: anuraggcp101@gmail.com

gcloud config get-value project
# should print: cerberus-491806
```

---

## Step 2 — One-time GCP setup

Run this **once**. It enables all required APIs and grants the service account the roles it needs:

```bash
cd access-mind
bash docs/setup_gcp.sh
```

What it enables:
| API | Why |
|---|---|
| `run.googleapis.com` | Cloud Run itself |
| `cloudbuild.googleapis.com` | Builds Docker image in the cloud (no local Docker needed) |
| `artifactregistry.googleapis.com` | Stores the built image |
| `secretmanager.googleapis.com` | Stores GEMINI_API_KEY securely |
| `cloudresourcemanager.googleapis.com` | List GCP projects |
| `iam.googleapis.com` | Grant / revoke IAM roles |
| `compute.googleapis.com` | List VMs and disks |
| `recommender.googleapis.com` | Cost saving recommendations |
| `storage.googleapis.com` | List Cloud Storage buckets |

---

## Step 3 — Deploy

```bash
export GEMINI_API_KEY=AIzaSy...your_key...
bash docs/deploy.sh
```

This will:
1. Store `GEMINI_API_KEY` in Secret Manager (creates or updates)
2. Build the Docker image using Cloud Build (in the cloud, ~3 min)
3. Deploy to Cloud Run with the SA attached
4. Print the live URL

Expected output:
```
==> Building and deploying via Cloud Build (no local Docker needed)...
Building and deploying from source...
...
Service [access-mind] revision [access-mind-00001-xxx] has been deployed
and is serving 100 percent of traffic.

==> Deployment complete!
==> Live URL:
https://access-mind-xxxxxxxxxx-uc.a.run.app
```

---

## Step 4 — Access the app

Open the URL printed by deploy.sh in a browser.

```bash
# Or get it any time with:
gcloud run services describe access-mind \
  --region=us-central1 \
  --format="value(status.url)"
```

The login screen will appear. Use the **🔐 anuraggcp101 (Admin)** quick-fill button.

---

## Step 5 — Observe & monitor

### Live logs (terminal)
```bash
gcloud run services logs tail access-mind \
  --region=us-central1 \
  --project=cerberus-491806
```

### Cloud Console (GUI)
1. Go to **console.cloud.google.com**
2. Navigate to **Cloud Run** → `access-mind`

You'll see four tabs:

| Tab | What to look at |
|---|---|
| **Metrics** | Request count, latency (p50/p95/p99), instance count, CPU/memory |
| **Logs** | All stdout/stderr from the app — FastAPI requests, agent steps, audit entries |
| **Revisions** | All deployed versions — roll back here if needed |
| **Security** | Verify SA is `cerberus-sa`, ingress is `All` (public) |

### View audit log from the app
```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="access-mind"' \
  --project=cerberus-491806 \
  --limit=50 \
  --format="value(textPayload)"
```

### Check Secret Manager
```bash
# Verify the Gemini key is stored
gcloud secrets versions list gemini-api-key --project=cerberus-491806
```

---

## Redeploying after code changes

Same command every time:
```bash
bash docs/deploy.sh
```

Cloud Build rebuilds only the changed layers. Takes ~2-3 min.

---

## Update the Gemini API key

```bash
echo -n "NEW_KEY_HERE" | gcloud secrets versions add gemini-api-key \
  --project=cerberus-491806 \
  --data-file=-

# Then redeploy to pick up the new secret version
bash docs/deploy.sh
```

---

## Rollback to a previous version

```bash
# List revisions
gcloud run revisions list --service=access-mind --region=us-central1

# Route 100% traffic to a specific revision
gcloud run services update-traffic access-mind \
  --region=us-central1 \
  --to-revisions=access-mind-00001-xxx=100
```

---

## Tear down (stop incurring costs)

Cloud Run only charges when requests are being served (scales to zero when idle). To fully remove:
```bash
gcloud run services delete access-mind \
  --region=us-central1 \
  --project=cerberus-491806
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails: `permission denied` | Run `setup_gcp.sh` again — a role grant may have timed out |
| App shows `mock mode` instead of GCP | SA not attached — check Cloud Run → Security tab shows `cerberus-sa` |
| `GEMINI_API_KEY` error on startup | Secret not created — re-run `deploy.sh` with `GEMINI_API_KEY` exported |
| 403 on IAM grant calls | SA needs `roles/resourcemanager.projectIamAdmin` — add via GCP Console |
| Cloud Build fails: `API not enabled` | Re-run `setup_gcp.sh` |
| App loads but Resources tab empty | SA may lack `roles/compute.viewer` — re-run `setup_gcp.sh` |
