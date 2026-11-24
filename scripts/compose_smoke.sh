#!/usr/bin/env bash
# Starts the whole Compose stack from a generated .env, checks it, pushes events and reads them back.
#
#   scripts/compose_smoke.sh                    # without a model: stops once events are stored
#   SMOKE_MODEL=1 scripts/compose_smoke.sh      # with the models in the template: reads records over /mcp
#
# Settings: CHECKCORD_API_PORT, CHECKCORD_WEB_PORT, CHECKCORD_POSTGRES_PORT, CHECKCORD_REDIS_PORT (non-default ports
# by default), SMOKE_ENV_TEMPLATE, SMOKE_UP_FLAGS (default --build), SMOKE_KEEP=1 to leave it running.
set -euo pipefail

cd "$(dirname "$0")/.."

export CHECKCORD_API_PORT="${CHECKCORD_API_PORT:-18000}"
export CHECKCORD_WEB_PORT="${CHECKCORD_WEB_PORT:-13000}"
export CHECKCORD_POSTGRES_PORT="${CHECKCORD_POSTGRES_PORT:-15432}"
export CHECKCORD_REDIS_PORT="${CHECKCORD_REDIS_PORT:-16379}"
template="${SMOKE_ENV_TEMPLATE:-.env.local-models.example}"
api="http://127.0.0.1:${CHECKCORD_API_PORT}"
model="${SMOKE_MODEL:-0}"
person="smoke@example.com"

if [[ -e .env ]]; then
  echo "A .env already exists; move it away first." >&2
  exit 2
fi

cleanup() {
  status=$?
  if [[ "${SMOKE_KEEP:-0}" != 1 ]]; then
    if [[ $status -ne 0 ]]; then docker compose logs --tail 80 api worker migrate || true; fi
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f .env
  fi
  exit $status
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }

# The first word on the first non-empty line after a line matching $1: how keys and secrets are shown.
shown_after() {
  awk -v pat="$1" '$0 ~ pat {found=1; next} found && NF {print $1; exit}'
}

wait_for() {
  local what=$1 tries=$2
  shift 2
  for _ in $(seq "$tries"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 5
  done
  echo "Timed out waiting for: $what" >&2
  return 1
}

step "generate .env from $template"
sed -e "s#^PUBLIC_BASE_URL=.*#PUBLIC_BASE_URL=${api}#" \
    -e "s#^WEB_BASE_URL=.*#WEB_BASE_URL=http://127.0.0.1:${CHECKCORD_WEB_PORT}#" \
    "$template" > .env

step "start the stack"
# shellcheck disable=SC2086
docker compose up -d --wait ${SMOKE_UP_FLAGS---build}
curl -fsS "$api/readyz"
echo
curl -fsS -o /dev/null "http://127.0.0.1:${CHECKCORD_WEB_PORT}/" && echo "console answers"

step "admin key and a person"
admin=$(docker compose exec -T api checkcord init | shown_after "^Created admin API key")
key=$(docker compose exec -T api checkcord keys create --name smoke --user "$person" --create-user \
  | shown_after "^Created API key")
[[ -n "$admin" && -n "$key" ]]

step "checkcord doctor"
doctor=$(docker compose exec -T api checkcord doctor || true)
echo "$doctor"
failed=$(grep '^FAIL' <<<"$doctor" || true)
if [[ "$model" != 1 ]]; then
  failed=$(grep -vE '^FAIL  (llm models|embedding endpoint)' <<<"$failed" || true)
fi
if [[ -n "$failed" ]]; then
  echo "doctor failed:" >&2
  echo "$failed" >&2
  exit 1
fi

step "push API: examples/push_events.py"
CHECKCORD_URL="$api" CHECKCORD_KEY="$admin" python3 examples/push_events.py

step "signed webhook: checkcord connect webhook, examples/signed_webhook.py"
connect=$(docker compose exec -T api checkcord connect webhook --name smoke-hook)
secret=$(shown_after "^Signing secret" <<<"$connect")
hook=$(grep -oE '/api/v1/webhooks/signed/[0-9a-f-]+' <<<"$connect" | head -1)
CHECKCORD_WEBHOOK_URL="$api$hook" CHECKCORD_WEBHOOK_SECRET="$secret" \
  python3 examples/signed_webhook.py --id smoke-incident --email "$person"

stored() {
  local n
  n=$(docker compose exec -T postgres psql -U checkcord -d checkcord -tAc \
    "SELECT count(*) FROM events WHERE source_id IN ('runbook-wiki/payments-retries', 'smoke-incident')")
  [[ "$n" == 2 ]]
}
step "events accepted and stored"
wait_for "both events in the events table" 36 stored
echo "both events are stored"

if [[ "$model" != 1 ]]; then
  echo
  echo "No model: records are not formed, so nothing is read back over /mcp. Set SMOKE_MODEL=1 to go on."
  exit 0
fi

readable() {
  CHECKCORD_URL="$api" CHECKCORD_KEY="$key" python3 examples/mcp_read.py "$1" \
    | python3 -c "import json, sys; found = json.load(sys.stdin)['search']; \
sys.exit(0 if any('$2' in r.get('summary', '') for r in found.get('results', [])) else 1)"
}
step "records readable over /mcp"
wait_for "the pushed record" 60 readable "payment retries" "retries"
echo "the pushed record is readable"
wait_for "the signed webhook record" 60 readable "card tokens retention" "card tokens"
echo "the signed webhook record is readable by $person"
