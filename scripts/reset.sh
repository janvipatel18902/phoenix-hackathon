#!/usr/bin/env bash
# Reseed the mock world and reset Mongo. Idempotent: safe to run any time.
#   scripts/reset.sh                  reseed mock + clear app collections (keeps recovery_memories)
#   scripts/reset.sh --drop-memories  also clear recovery_memories
#   SKIP_MONGO=1 scripts/reset.sh     mock world only (no .env / Atlas needed)
set -euo pipefail
cd "$(dirname "$0")/.."

# Load .env, but never override variables already set in the environment
# (so `MONGODB_DB=scratch scripts/reset.sh` really targets "scratch").
if [ -f .env ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    case "$key" in ''|\#*) continue ;; esac
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    if [ -z "${!key+x}" ]; then export "$key=$value"; fi
  done < .env
fi
MOCK_URL="${MOCK_WORLD_URL:-http://localhost:8001}"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY="${PYTHON:-python3}"; fi

echo "Reseeding mock world at $MOCK_URL ..."
curl -fsS --max-time 10 -X POST "$MOCK_URL/admin/reset" \
  -H 'Content-Type: application/json' \
  -d '{"event_id": "evt_concert_001", "attendees": 200, "seed": 1}' >/dev/null \
  || { echo "mock_world is not reachable: start it with 'uvicorn mock_world.app:app --port 8001'" >&2; exit 1; }
echo "mock world reseeded: $(curl -fsS --max-time 10 "$MOCK_URL/admin/reality")"

if [ "${SKIP_MONGO:-0}" = "1" ]; then
  echo "SKIP_MONGO=1: leaving Mongo untouched."
  exit 0
fi
if [ -z "${MONGODB_URI:-}" ]; then
  echo "MONGODB_URI is not set (put it in .env), or run with SKIP_MONGO=1." >&2
  exit 1
fi
PYTHONPATH=. "$PY" scripts/reset_db.py "$@"
