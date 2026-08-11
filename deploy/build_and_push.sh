#!/bin/bash
# Build the worker container and push to Artifact Registry. Run from Cloud Shell.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-youtube-podcast-sync-502121}"
REGION="${REGION:-us-east1}"
REPO="${REPO:-autoclip-images}"
IMAGE="${IMAGE:-autoclip-worker}"
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"

FULL_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE}:${TAG}"
LATEST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE}:latest"

echo "Building ${FULL_IMAGE}"
cd "$(dirname "$0")/../worker"

gcloud builds submit \
  --tag "${FULL_IMAGE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}"

gcloud artifacts docker tags add "${FULL_IMAGE}" "${LATEST}" --quiet

echo ""
echo "Built: ${FULL_IMAGE}"
echo "Tagged latest: ${LATEST}"
