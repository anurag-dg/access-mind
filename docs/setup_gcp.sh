#!/usr/bin/env bash
# setup_gcp.sh — One-time GCP setup for Access Mind
# Run this ONCE before your first deploy.
#
# Usage: bash docs/setup_gcp.sh
# Requires: gcloud CLI authenticated as anuraggcp101@gmail.com

set -euo pipefail

PROJECT_ID="cerberus-491806"
SA_EMAIL="cerberus-sa@cerberus-491806.iam.gserviceaccount.com"
REGION="us-central1"

echo "==> Setting project to ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}"

echo "==> Enabling required APIs (takes ~2 min on first run)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  compute.googleapis.com \
  recommender.googleapis.com \
  storage.googleapis.com \
  --project="${PROJECT_ID}"

echo "==> Granting service account roles needed by the app..."
for ROLE in \
  roles/resourcemanager.projectViewer \
  roles/iam.securityReviewer \
  roles/compute.viewer \
  roles/storage.objectViewer \
  roles/recommender.viewer \
  roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet
  echo "  Granted ${ROLE}"
done

echo "==> Granting Cloud Run SA the ability to act as itself (for source deploy)..."
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet

echo "==> Granting your account permission to deploy Cloud Run services..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="user:anuraggcp101@gmail.com" \
  --role="roles/run.admin" \
  --quiet

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="user:anuraggcp101@gmail.com" \
  --role="roles/cloudbuild.builds.editor" \
  --quiet

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="user:anuraggcp101@gmail.com" \
  --role="roles/iam.serviceAccountUser" \
  --quiet

echo ""
echo "==> Setup complete! Now run:"
echo "    export GEMINI_API_KEY=your_key"
echo "    bash docs/deploy.sh"
