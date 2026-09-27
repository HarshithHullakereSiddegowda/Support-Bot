#!/usr/bin/env bash
# Ask the DEPLOYED bot a question and show what it did.
#
#   ./ask-prod.sh "How do I pair a Bluetooth device?"
#
# Local equivalent is ./ask.sh -- same graph, different address.
set -euo pipefail
cd "$(dirname "$0")"

QUERY="${1:?usage: ./ask-prod.sh \"your question\"}"
VENV=../.venv/bin/python

# .env has JWT_SECRET; .env.prod has CLOUD_RUN_URL.
set -a; . ./.env; . ./.env.prod; set +a
URL="${CLOUD_RUN_URL}"

# Same secret the deployed app verifies with, so the same token works.
TOKEN=$($VENV -c "import os;from jose import jwt;print(jwt.encode({'sub':'dev'},os.environ['JWT_SECRET'],algorithm='HS256'))")

echo "→ $URL"
echo "→ $QUERY"
echo "  (cold start adds ~20s if nobody has called it recently)"
echo

START=$(date +%s)
curl -s --max-time 290 -X POST "$URL/query" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'X-Bypass-Cache: true' \
  -d "$($VENV -c "import json,sys;print(json.dumps({'query':sys.argv[1]}))" "$QUERY")" \
  > /tmp/ask_prod.json
END=$(date +%s)
echo "round trip: $((END-START))s"
echo

$VENV - <<'PY'
import json
d = json.load(open("/tmp/ask_prod.json"))
if "detail" in d:
    print("FAILED:", d["detail"])
    print("  logs:  ./prod-logs.sh")
    raise SystemExit(1)
print("=" * 60)
print(f"  model        {d['model_used']}")
print(f"  faithfulness {d['faithfulness_score']:.3f}   grounded in the Apple guide?")
print(f"  completeness {d['completeness_score']:.3f}   answered everything?")
print(f"  passed       {d['validation_passed']}")
print("=" * 60)
print()
print(d["response"])
PY
