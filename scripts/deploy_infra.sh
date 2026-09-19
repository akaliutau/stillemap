#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"
load_deploy_env

printf '[infra] project=%s region=%s repository=%s service_account=%s\n' \
  "$PROJECT_ID" "$REGION" "$REPOSITORY" "$SA"

gcloud config set project "$PROJECT_ID" >/dev/null

printf '[infra] enabling APIs\n'
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  logging.googleapis.com \
  geocoding-backend.googleapis.com \
  weather.googleapis.com \
  --project "$PROJECT_ID"

if ! gcloud artifacts repositories describe "$REPOSITORY" \
  --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  printf '[infra] creating Artifact Registry repository: %s\n' "$REPOSITORY"
  gcloud artifacts repositories create "$REPOSITORY" \
    --repository-format=docker \
    --location="$REGION" \
    --description="StilleMap Cloud Run images" \
    --project="$PROJECT_ID"
else
  printf '[infra] reuse Artifact Registry repository: %s\n' "$REPOSITORY"
fi

if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  printf '[infra] creating runtime service account: %s\n' "$SA"
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="StilleMap runtime" \
    --project="$PROJECT_ID"
else
  printf '[infra] reuse runtime service account: %s\n' "$SA"
fi

sleep 5

# Gemini through Vertex AI uses Cloud Run service identity / ADC.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/aiplatform.user" \
  --condition=None >/dev/null

# Cloud Build's default service account changed for newer projects. Ask GCP which
# identity is actually used, then grant that identity permission to push images.
BUILD_SA="$(gcloud builds get-default-service-account \
  --project "$PROJECT_ID" \
  --format='value(serviceAccountEmail)' 2>/dev/null || true)"
BUILD_SA="${BUILD_SA##*/}"
if [[ -n "$BUILD_SA" ]]; then
  printf '[infra] Cloud Build service account: %s\n' "$BUILD_SA"
  gcloud artifacts repositories add-iam-policy-binding "$REPOSITORY" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${BUILD_SA}" \
    --role="roles/artifactregistry.writer" >/dev/null
else
  printf '[infra] WARNING: could not resolve Cloud Build default service account; first build may need IAM adjustment\n' >&2
fi

# Secrets are optional by design. If absent, the app keeps those data sources as None.
sync_optional_secret_from_env GOOGLE_MAPS_API_KEY "$GOOGLE_MAPS_SECRET_NAME"
sync_optional_secret_from_env TFL_APP_KEY "$TFL_SECRET_NAME"

# Only needed when GOOGLE_GENAI_USE_VERTEXAI=false.
if [[ "${GOOGLE_GENAI_USE_VERTEXAI,,}" == "false" ]]; then
  sync_optional_secret_from_env GEMINI_API_KEY "$GEMINI_SECRET_NAME"
fi

printf '\nInfrastructure ready.\n'
printf '  Artifact Registry: %s-docker.pkg.dev/%s/%s\n' "$REGION" "$PROJECT_ID" "$REPOSITORY"
printf '  Runtime service account: %s\n' "$SA"
printf '  Vertex AI: project=%s location=%s\n' "$PROJECT_ID" "$GOOGLE_CLOUD_LOCATION"
printf 'Next: scripts/deploy_service.sh\n'
