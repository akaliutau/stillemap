#!/usr/bin/env bash
set -euo pipefail

load_env_file() {
  local file="${ENV_FILE:-.env}"
  [[ -f "$file" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *"="* ]] || continue

    local key="${line%%=*}"
    local value="${line#*=}"
    key="$(printf '%s' "$key" | xargs)"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!key-}" ]] && continue

    value="${value#${value%%[![:space:]]*}}"
    value="${value%${value##*[![:space:]]}}"
    if [[ "$value" =~ ^\".*\"$ || "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$file"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'ERROR: required command is not installed: %s\n' "$1" >&2
    exit 1
  }
}

load_deploy_env() {
  load_env_file
  require_command gcloud

  PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
  PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID or GOOGLE_CLOUD_PROJECT in .env}"
  REGION="${REGION:-europe-west2}"
  REPOSITORY="${REPOSITORY:-main-repo}"
  SERVICE_NAME="${SERVICE_NAME:-stillemap}"
  IMAGE_NAME="${IMAGE_NAME:-app}"
  SA_NAME="${SA_NAME:-stillemap-run}"
  SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

  GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
  GOOGLE_GENAI_USE_VERTEXAI="${GOOGLE_GENAI_USE_VERTEXAI:-true}"
  GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.8-flash}"

  CLOUD_RUN_CPU="${CLOUD_RUN_CPU:-4}"
  CLOUD_RUN_MEMORY="${CLOUD_RUN_MEMORY:-8Gi}"
  CLOUD_RUN_CONCURRENCY="${CLOUD_RUN_CONCURRENCY:-1}"
  CLOUD_RUN_TIMEOUT="${CLOUD_RUN_TIMEOUT:-900}"
  CLOUD_RUN_MIN_INSTANCES="${CLOUD_RUN_MIN_INSTANCES:-1}"
  CLOUD_RUN_MAX_INSTANCES="${CLOUD_RUN_MAX_INSTANCES:-2}"
  CLOUD_RUN_ALLOW_UNAUTHENTICATED="${CLOUD_RUN_ALLOW_UNAUTHENTICATED:-true}"

  GOOGLE_MAPS_SECRET_NAME="${GOOGLE_MAPS_SECRET_NAME:-stillemap-google-maps-api-key}"
  TFL_SECRET_NAME="${TFL_SECRET_NAME:-stillemap-tfl-app-key}"
  GEMINI_SECRET_NAME="${GEMINI_SECRET_NAME:-stillemap-gemini-api-key}"

  export PROJECT_ID REGION REPOSITORY SERVICE_NAME IMAGE_NAME SA_NAME SA
  export GOOGLE_CLOUD_LOCATION GOOGLE_GENAI_USE_VERTEXAI GEMINI_MODEL
  export CLOUD_RUN_CPU CLOUD_RUN_MEMORY CLOUD_RUN_CONCURRENCY CLOUD_RUN_TIMEOUT
  export CLOUD_RUN_MIN_INSTANCES CLOUD_RUN_MAX_INSTANCES CLOUD_RUN_ALLOW_UNAUTHENTICATED
  export GOOGLE_MAPS_SECRET_NAME TFL_SECRET_NAME GEMINI_SECRET_NAME
}

secret_exists() {
  gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1
}

sync_optional_secret_from_env() {
  local env_name="$1"
  local secret_name="$2"
  local value="${!env_name-}"

  if [[ -n "$value" ]]; then
    local tmp
    tmp="$(mktemp)"
    chmod 600 "$tmp"
    printf '%s' "$value" > "$tmp"

    if secret_exists "$secret_name"; then
      printf '[secret] add version: %s <- %s\n' "$secret_name" "$env_name"
      gcloud secrets versions add "$secret_name" \
        --data-file="$tmp" \
        --project="$PROJECT_ID" >/dev/null
    else
      printf '[secret] create: %s <- %s\n' "$secret_name" "$env_name"
      gcloud secrets create "$secret_name" \
        --replication-policy=automatic \
        --data-file="$tmp" \
        --project="$PROJECT_ID" >/dev/null
    fi
    rm -f "$tmp"
  elif secret_exists "$secret_name"; then
    printf '[secret] reuse existing: %s\n' "$secret_name"
  else
    printf '[secret] skip: %s is unset and %s does not exist\n' "$env_name" "$secret_name"
    return 0
  fi

  gcloud secrets add-iam-policy-binding "$secret_name" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}
