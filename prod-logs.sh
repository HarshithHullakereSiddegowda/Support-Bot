#!/usr/bin/env bash
# Show the deployed app's structlog output, newest last.
#   ./prod-logs.sh          last 30 lines
#   ./prod-logs.sh 100      last 100
set -euo pipefail
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="support-bot"' \
  --limit "${1:-30}" --project supportbot-509809 \
  --format='value(textPayload,jsonPayload.message)' 2>/dev/null \
  | grep -vE '^\s*$' | tail -r
