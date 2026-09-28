#!/usr/bin/env bash
# Build, push and deploy the app to Cloud Run.
#
#   ./deploy.sh v3           build + push + deploy tag v3
#   ./deploy.sh v3 --no-build   deploy an already-pushed tag
#
# Secrets come from .env and .env.prod (both gitignored). They are written to a
# temp file only because gcloud's --set-env-vars splits on a delimiter, and every
# candidate delimiter (comma, @) appears inside our connection strings. The temp
# file is deleted on exit, success or failure.
set -euo pipefail
cd "$(dirname "$0")"

TAG="${1:?usage: ./deploy.sh <tag> [--no-build]}"
NO_BUILD="${2:-}"

PROJECT=supportbot-509809
REGION=australia-southeast1
SERVICE=support-bot
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${SERVICE}/app:${TAG}"
VENV=../.venv/bin/python

set -a; . ./.env; . ./.env.prod; set +a

# ── env file, deleted on exit ────────────────────────────────────────────────
ENV_FILE=$(mktemp -t cloudrun-env.XXXXXX)
trap 'rm -f "$ENV_FILE"' EXIT

$VENV - "$ENV_FILE" <<'PY'
import json, os, sys
# Values that are the same in every environment.
out = {
    "LANGCHAIN_TRACING_V2":   "true",
    "LANGCHAIN_PROJECT":      "apple-support-bot-prod",
    "LOW_COMPLEXITY_MODEL":   os.environ.get("LOW_COMPLEXITY_MODEL",  "gemini-3.1-flash-lite"),
    "HIGH_COMPLEXITY_MODEL":  os.environ.get("HIGH_COMPLEXITY_MODEL", "gemini-3.5-flash"),
    "UTILITY_MODEL":          os.environ.get("UTILITY_MODEL",         "gemini-3.1-flash-lite"),
    "PROMPT_VERSION":         os.environ.get("PROMPT_VERSION",        "v2"),
    # Not deployed. The breakers handle both: rival fails open, cache is skipped.
    # Named "-unavailable" so the logs are honest about why the breaker opened.
    "RIVAL_URL":              "http://rival-unavailable:8002",
    "GPTCACHE_URL":           "http://gptcache-unavailable:8000",
}
# Secrets and connection strings, from the environment. Fail loudly if absent --
# every one of these is read at import time, so a missing value means the
# container dies on startup rather than on first request.
for k in ("GOOGLE_API_KEY", "OPENAI_API_KEY", "LANGCHAIN_API_KEY", "JWT_SECRET",
          "PAGEINDEX_API_KEY", "MONGODB_URI", "POSTGRES_DSN",
          # Password the /chat page exchanges for a JWT. Without it /auth/token
          # returns 503 and the UI cannot log anyone in.
          "APP_PASSWORD"):
    v = os.environ.get(k)
    if not v:
        sys.exit(f"ERROR: {k} is not set. Check .env and .env.prod.")
    out[k] = v
# json is valid YAML for a flat string map, and quotes every value correctly.
with open(sys.argv[1], "w") as f:
    json.dump(out, f, indent=2)
print(f"  {len(out)} env vars prepared")
PY

# ── build and push ──────────────────────────────────────────────────────────
if [ "$NO_BUILD" != "--no-build" ]; then
  echo "building $IMAGE"
  echo "  platform linux/amd64 (this Mac is $(uname -m); Cloud Run is x86)"
  docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
else
  echo "skipping build, deploying existing $IMAGE"
fi

# ── deploy ──────────────────────────────────────────────────────────────────
# --memory 4Gi     Presidio loads spaCy en_core_web_lg (~600MB) per worker
# --concurrency 4  each request is CPU-heavy and holds a Postgres connection
# --min-instances 0  scale to zero = no idle cost; costs a ~20s cold start
# --max-instances 3  spending cap
# --allow-unauthenticated  removes Google's IAM layer; our JWT middleware still guards /query
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --port 8000 \
  --memory 4Gi \
  --cpu 2 \
  --timeout 300 \
  --concurrency 4 \
  --min-instances 0 \
  --max-instances 3 \
  --allow-unauthenticated \
  --env-vars-file "$ENV_FILE"

echo
echo "deployed. smoke test:"
curl -s --max-time 120 "$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')/health"
echo
