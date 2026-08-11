#!/bin/bash
# Deploy the autoclip-worker Cloud Run Service with L4 GPU. Run from Cloud Shell.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-youtube-podcast-sync-502121}"
REGION="${REGION:-us-east1}"
SERVICE="${SERVICE:-autoclip-worker}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/autoclip-images/autoclip-worker:latest}"
WORKER_SA="${WORKER_SA:-autoclip-worker@${PROJECT_ID}.iam.gserviceaccount.com}"

if [ -z "${WORKER_SHARED_SECRET:-}" ]; then
  echo "ERROR: WORKER_SHARED_SECRET must be set"
  echo "e.g. export WORKER_SHARED_SECRET=\$(cat ~/.autoclip_worker_secret)"
  exit 1
fi

gcloud run deploy "${SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${WORKER_SA}" \
  --gpu 1 \
  --gpu-type nvidia-l4 \
  --no-cpu-throttling \
  --cpu 4 \
  --memory 16Gi \
  --timeout 3600 \
  --concurrency 1 \
  --min-instances 0 \
  --max-instances 5 \
  --no-allow-unauthenticated \
  --set-env-vars "VM_CALLBACK_URL=https://autoclip.cloud,WORKER_SHARED_SECRET=${WORKER_SHARED_SECRET},OUTPUT_GCS_BUCKET=autoclip-uploads"

echo ""
echo "Deployed ${SERVICE}. URL:"
gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --format="value(status.url)"
