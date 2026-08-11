#!/bin/bash
# One-time: create service accounts + grant GCS access. Run from Cloud Shell.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-youtube-podcast-sync-502121}"
BUCKET="${BUCKET:-autoclip-uploads}"

gcloud iam service-accounts create autoclip-worker \
  --project "${PROJECT_ID}" \
  --display-name "AutoClip worker (Cloud Run + GPU)" \
  --quiet 2>/dev/null || echo "  autoclip-worker exists"

gcloud iam service-accounts create autoclip-tasks-invoker \
  --project "${PROJECT_ID}" \
  --display-name "AutoClip Cloud Tasks invoker" \
  --quiet 2>/dev/null || echo "  autoclip-tasks-invoker exists"

WORKER_SA="autoclip-worker@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER_SA="autoclip-tasks-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Granting bucket access..."
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${WORKER_SA}" \
  --role "roles/storage.objectAdmin" \
  --quiet

echo ""
echo "Service accounts ready:"
echo "  Worker:  ${WORKER_SA}"
echo "  Invoker: ${INVOKER_SA}"
