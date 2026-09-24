#!/usr/bin/env bash
# Ask the bot one question and show what the graph did.
#
#   ./ask.sh "How do I set up Face ID?"
#
set -euo pipefail
cd "$(dirname "$0")"

QUERY="${1:?usage: ./ask.sh \"your question\"}"
VENV=../.venv/bin/python

# 1. Load .env so JWT_SECRET is available to mint a token.
set -a; . ./.env; set +a

# 2. Mint a dev JWT. auth_middleware decodes this with the same secret.
TOKEN=$($VENV -c "import os;from jose import jwt;print(jwt.encode({'sub':'dev'},os.environ['JWT_SECRET'],algorithm='HS256'))")

echo "→ asking: $QUERY"
echo "  (30-60s: two LLM calls for safety+intent, retrieval, generation, two judges)"
echo

# 3. Send it. 300s timeout — the judges are slow.
curl -s --max-time 300 -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$($VENV -c "import json,sys;print(json.dumps({'query':sys.argv[1]}))" "$QUERY")" \
  > /tmp/ask_result.json

# 4. Show the scores and the answer.
$VENV - <<'PY'
import json
d = json.load(open("/tmp/ask_result.json"))
if "detail" in d:
    print("REQUEST FAILED:", d["detail"])
    print("  check why with:  docker compose logs app | tail -20")
    raise SystemExit(1)
print("=" * 62)
print(f"  model        {d['model_used']}")
print(f"  faithfulness {d['faithfulness_score']:.3f}   (is it grounded in the PDF?)")
print(f"  completeness {d['completeness_score']:.3f}   (did it answer everything?)")
print(f"  passed       {d['validation_passed']}")
print("=" * 62)
print()
print(d["response"])
open("/tmp/ask_rid", "w").write(d["request_id"])
PY

# 5. Replay the graph trace for THIS request, from the JSON logs.
echo
echo "───────────── what the graph did ─────────────"
RID=$(cat /tmp/ask_rid)
docker compose logs app 2>/dev/null | grep "$RID" | $VENV -c "
import sys, json, re, datetime
rows = []
for line in sys.stdin:
    try: rows.append(json.loads(re.sub(r'^app-1\s*\|\s*', '', line.strip())))
    except Exception: pass
t0 = None
for d in rows:
    ts = datetime.datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00'))
    t0 = t0 or ts
    extra = ' '.join(f'{k}={d[k]}' for k in
                     ('num_nodes','score','complexity','num_sub_queries','pii_found')
                     if k in d)
    print(f\"{(ts-t0).total_seconds():6.2f}s  {d.get('node','-'):20} {d['event']} {extra}\")
"
