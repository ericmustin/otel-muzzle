#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Sends the example stack's traces to Elastic twice, with otel-muzzle off and then on, so Kibana can show the two
# side by side as the environments otel-muzzle-off and otel-muzzle-on. Both runs use the same apps under each
# language's zero-code agent; "off" turns otel-muzzle off with each language's own switch
# (examples/compose/muzzle-off.yml).
#
# Needs, in the environment (never written to disk by this script):
#   ELASTIC_OTLP_ENDPOINT   Elastic's OTLP endpoint, e.g. https://<project>.ingest.<region>.<csp>.elastic.cloud:443
#   ELASTIC_API_KEY         an API key that can write traces there
# Writes the trace ids and time windows to examples/backends/elastic/out/run.json for check.py and capture.mjs.
#
#   REQUESTS=60 examples/backends/elastic/demo.sh
set -euo pipefail
cd "$(dirname "$0")/../../.."

: "${ELASTIC_OTLP_ENDPOINT:?set ELASTIC_OTLP_ENDPOINT}" "${ELASTIC_API_KEY:?set ELASTIC_API_KEY}"
REQUESTS="${REQUESTS:-60}"
OUT=examples/backends/elastic/out/run.json
log() { printf '[demo %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# One empty OTLP request first: rejected credentials stop here, not as an export error per batch. The header
# reaches curl through a file descriptor, so the key never appears in the process list.
status="$(curl -s -o /dev/null -m 15 -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -H @<(printf 'Authorization: ApiKey %s\n' "$ELASTIC_API_KEY") --data '{"resourceSpans":[]}' \
  "${ELASTIC_OTLP_ENDPOINT%/}/v1/traces" || true)"
[[ "$status" == 2* ]] || { log "Elastic rejected the OTLP preflight (HTTP ${status:-no response}); check the key"; exit 1; }
log "Elastic accepted the OTLP preflight"

rm -f "$OUT"
for variant in off on; do
  files=(-f docker-compose.yml -f examples/compose/zerocode.yml -f examples/backends/elastic/compose.yml)
  [[ "$variant" == off ]] && files+=(-f examples/compose/muzzle-off.yml)
  export ELASTIC_ENVIRONMENT="otel-muzzle-${variant}"
  log "otel-muzzle ${variant}: starting the stack as ${ELASTIC_ENVIRONMENT}"
  docker compose "${files[@]}" up -d --build --wait --quiet-pull >/dev/null 2>&1 \
    || { docker compose "${files[@]}" logs --tail 40; docker compose "${files[@]}" down -v; exit 1; }
  python3 examples/backends/elastic/traffic.py --variant "$variant" --environment "$ELASTIC_ENVIRONMENT" \
    --requests "$REQUESTS" --out "$OUT"
  # Longer than the SDKs' 5 s export delay, then the collector's own batch.
  sleep 10
  docker compose "${files[@]}" down -v >/dev/null 2>&1
done
log "done: $OUT. Next: examples/backends/elastic/check.py, then capture.mjs (see examples/backends/elastic/README.md)"
