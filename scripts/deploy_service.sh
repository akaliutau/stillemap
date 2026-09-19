#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"
load_deploy_env

TAG="${TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:${TAG}}"

OSM_SOURCE="${OSM_SOURCE:-local_gpkg}"
OSM_LOCAL_GPKG_PATH="${OSM_LOCAL_GPKG_PATH:-src/stillemap/data/greater-london.gpkg}"

if [[ "$OSM_SOURCE" == "local_gpkg" && ! -f "${ROOT_DIR}/${OSM_LOCAL_GPKG_PATH}" ]]; then
  printf 'ERROR: local OSM cache is missing: %s\n' "${ROOT_DIR}/${OSM_LOCAL_GPKG_PATH}" >&2
  printf 'Run: scripts/download_osm_cache.sh\n' >&2
  exit 1
fi

if ! gcloud artifacts repositories describe "$REPOSITORY" \
  --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  printf 'ERROR: Artifact Registry repository %s does not exist. Run scripts/deploy_infra.sh first.\n' "$REPOSITORY" >&2
  exit 1
fi

printf '[build] image=%s\n' "$IMAGE"
(
  cd "$ROOT_DIR"
  gcloud builds submit \
    --tag "$IMAGE" \
    --project "$PROJECT_ID" \
    .
)

# Runtime config intentionally overrides the local Docker-based NoiseModelling mode:
# Cloud Run cannot launch sibling Docker containers, so the service image embeds NM.
RUNTIME_ENV="GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION},GEMINI_MODEL=${GEMINI_MODEL},NM_MODE=local,NM_LOCAL_HOME=/opt/noisemodelling,RUNS_DIR=/tmp/stillemap/runs,DEBUG=${DEBUG:-true},TFL_CAMERA_RADIUS_M=${TFL_CAMERA_RADIUS_M:-1000},DFT_YEAR=${DFT_YEAR:-2025},DFT_REGION_NAME=${DFT_REGION_NAME:-London},DFT_PAGE_SIZE=${DFT_PAGE_SIZE:-1000},DFT_NEARBY_RADIUS_M=${DFT_NEARBY_RADIUS_M:-1500},DFT_ROAD_MATCH_RADIUS_M=${DFT_ROAD_MATCH_RADIUS_M:-60},DFT_MAX_POINTS=${DFT_MAX_POINTS:-40},OSM_RADIUS_M=${OSM_RADIUS_M:-450},OSM_SOURCE=${OSM_SOURCE},OSM_LOCAL_GPKG_PATH=${OSM_LOCAL_GPKG_PATH},SIM_RADIUS_M=${SIM_RADIUS_M:-400},RECEIVER_GRID_M=${RECEIVER_GRID_M:-25},RECEIVER_HEIGHT_M=${RECEIVER_HEIGHT_M:-4},TARGET_EPSG=${TARGET_EPSG:-27700},NOISE_MAX_SOURCE_DISTANCE_M=${NOISE_MAX_SOURCE_DISTANCE_M:-300},NOISE_REFLECTION_ORDER=${NOISE_REFLECTION_ORDER:-0},NOISE_DIFF_HORIZONTAL=${NOISE_DIFF_HORIZONTAL:-false},NOISE_DIFF_VERTICAL=${NOISE_DIFF_VERTICAL:-false},TRAFFIC_DAY_SHARE=${TRAFFIC_DAY_SHARE:-0.70},TRAFFIC_EVENING_SHARE=${TRAFFIC_EVENING_SHARE:-0.20},TRAFFIC_NIGHT_SHARE=${TRAFFIC_NIGHT_SHARE:-0.10},TRAFFIC_MISSING_POLICY=${TRAFFIC_MISSING_POLICY:-skip},AI_LIVE_TRAFFIC_ENABLED=${AI_LIVE_TRAFFIC_ENABLED:-true},AI_MIN_CONFIDENCE=${AI_MIN_CONFIDENCE:-0.55},NM_TIMEOUT_SEC=${NM_TIMEOUT_SEC:-600},HTTP_TIMEOUT_SEC=${HTTP_TIMEOUT_SEC:-30}"

SECRET_BINDINGS=()
if secret_exists "$GOOGLE_MAPS_SECRET_NAME"; then
  SECRET_BINDINGS+=("GOOGLE_MAPS_API_KEY=${GOOGLE_MAPS_SECRET_NAME}:latest")
fi
if secret_exists "$TFL_SECRET_NAME"; then
  SECRET_BINDINGS+=("TFL_APP_KEY=${TFL_SECRET_NAME}:latest")
fi
if [[ "${GOOGLE_GENAI_USE_VERTEXAI,,}" == "false" ]] && secret_exists "$GEMINI_SECRET_NAME"; then
  SECRET_BINDINGS+=("GEMINI_API_KEY=${GEMINI_SECRET_NAME}:latest")
fi
SECRET_ARGS=()
if (( ${#SECRET_BINDINGS[@]} > 0 )); then
  SECRET_CSV="$(IFS=,; echo "${SECRET_BINDINGS[*]}")"
  SECRET_ARGS=(--set-secrets "$SECRET_CSV")
fi

AUTH_ARGS=(--no-allow-unauthenticated)
if [[ "${CLOUD_RUN_ALLOW_UNAUTHENTICATED,,}" == "true" ]]; then
  AUTH_ARGS=(--allow-unauthenticated)
fi

printf '[deploy] service=%s cpu=%s memory=%s concurrency=%s timeout=%ss\n' \
  "$SERVICE_NAME" "$CLOUD_RUN_CPU" "$CLOUD_RUN_MEMORY" "$CLOUD_RUN_CONCURRENCY" "$CLOUD_RUN_TIMEOUT"

gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA" \
  --cpu "$CLOUD_RUN_CPU" \
  --memory "$CLOUD_RUN_MEMORY" \
  --concurrency "$CLOUD_RUN_CONCURRENCY" \
  --timeout "$CLOUD_RUN_TIMEOUT" \
  --min-instances "$CLOUD_RUN_MIN_INSTANCES" \
  --max-instances "$CLOUD_RUN_MAX_INSTANCES" \
  --set-env-vars "$RUNTIME_ENV" \
  "${SECRET_ARGS[@]}" \
  "${AUTH_ARGS[@]}" \
  --project "$PROJECT_ID"

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format='value(status.url)')"

printf '\nDeployed Cloud Run service: %s\n' "$SERVICE_NAME"
printf 'URL: %s\n' "$SERVICE_URL"

if command -v curl >/dev/null 2>&1; then
  printf '[smoke] GET /health\n'
  if [[ "${CLOUD_RUN_ALLOW_UNAUTHENTICATED,,}" == "true" ]]; then
    curl -fsS "${SERVICE_URL}/health" && printf '\n'
  else
    curl -fsS \
      -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
      "${SERVICE_URL}/health" && printf '\n'
  fi
else
  printf '[smoke] curl not installed; skipped HTTP health check\n'
fi

printf 'Preflight: %s/preflight\n' "$SERVICE_URL"
