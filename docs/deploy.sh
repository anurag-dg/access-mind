#!/usr/bin/env bash
# deploy.sh — Deploy Access Mind to Google Cloud Run
# Uses Cloud Build (no local Docker needed) + SA attachment (no JSON key in env vars)
#
# Usage: bash docs/deploy.sh
# Pre-requisites: run docs/setup_gcp.sh once first

set -euo pipefail

PROJECT_ID="cerberus-491806"
REGION="us-central1"
SERVICE_NAME="access-mind"
SA_EMAIL="cerberus-sa@cerberus-491806.iam.gserviceaccount.com"

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "ERROR: GEMINI_API_KEY is not set. Export it first:"
  echo "  export GEMINI_API_KEY=your_key_here"
  exit 1
fi

echo "==> Project : ${PROJECT_ID}"
echo "==> Region  : ${REGION}"
echo "==> Service : ${SERVICE_NAME}"
echo ""

# Store GEMINI_API_KEY in Secret Manager (idempotent — updates if already exists)
echo "==> Storing GEMINI_API_KEY in Secret Manager..."
echo -n "${GEMINI_API_KEY}" | gcloud secrets create gemini-api-key \
  --project="${PROJECT_ID}" \
  --data-file=- \
  --replication-policy=automatic 2>/dev/null \
|| echo -n "${GEMINI_API_KEY}" | gcloud secrets versions add gemini-api-key \
  --project="${PROJECT_ID}" \
  --data-file=-

# Grant the SA access to read the secret
gcloud secrets add-iam-policy-binding gemini-api-key \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet

echo "==> Building and deploying via Cloud Build (no local Docker needed)..."
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  --port=8080 \
  --memory=512Mi \
  --cpu=1 \
  --service-account="${SA_EMAIL}" \
  --set-secrets="GEMINI_API_KEY=gemini-api-key:latest" \
  --quiet

echo ""
echo "==> Deployment complete!"
echo "==> Live URL:"
gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)"
