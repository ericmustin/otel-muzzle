#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Runs the otel-muzzle benchmark: the same k6 load against OpenTelemetry Demo 3.1.0 services with
# stock SDKs and with otel-muzzle, then reports spans, trace size and OTLP bytes per service.
# Everything runs in local Docker. See bench/README.md.
#
# Knobs (environment variables):
#   RATE=25                requests per second, per service, during the measured phase
#   DURATION=40s           length of the measured phase, per variant
#   WARMUP_RATE=5          requests per second during the (discarded) warmup phase
#   WARMUP_DURATION=10s
#   VARIANTS="baseline muzzle-0 muzzle-10 muzzle-25 muzzle-50 muzzle-100 header-10"
#   PROJECT_RPS=100        requests/s per service used for the GB/day projection
#   LOCAL_ONLY=0           1 = never forward spans, even if ../.env sets OTLP_FORWARD_ENDPOINT
#   SKIP_BUILD=0           1 = reuse the muzzle-bench/* images already built
set -euo pipefail
cd "$(dirname "$0")"

RATE="${RATE:-25}"
DURATION="${DURATION:-40s}"
WARMUP_RATE="${WARMUP_RATE:-5}"
WARMUP_DURATION="${WARMUP_DURATION:-10s}"
VARIANTS="${VARIANTS:-baseline muzzle-0 muzzle-10 muzzle-25 muzzle-50 muzzle-100 header-10}"
PROJECT_RPS="${PROJECT_RPS:-100}"
LOCAL_ONLY="${LOCAL_ONLY:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"
FLUSH_WAIT="${FLUSH_WAIT:-8}"   # seconds; longer than the SDKs' default 5 s batch delay
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

DEMO_VERSION=3.1.0
STOCK_PREFIX="ghcr.io/open-telemetry/demo:${DEMO_VERSION}"
BENCH_PREFIX="muzzle-bench"
APPS=(email payment recommendation)

export BENCH_UID="$(id -u)" BENCH_GID="$(id -g)"
export BENCH_OUT="./out/${RUN_ID}"
RUN_DIR="out/${RUN_ID}"

log() { printf '\n[bench %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
compose() { docker compose "$@"; }

# name -> "label|images|ratio|header_ratio"
variant_def() {
  case "$1" in
    baseline)   echo "Stock SDKs (baseline)|stock|0|0" ;;
    muzzle-0)   echo "otel-muzzle, ratio 0%|muzzle|0|0" ;;
    muzzle-10)  echo "otel-muzzle, ratio 10% (default)|muzzle|0.1|0" ;;
    muzzle-25)  echo "otel-muzzle, ratio 25%|muzzle|0.25|0" ;;
    muzzle-50)  echo "otel-muzzle, ratio 50%|muzzle|0.5|0" ;;
    muzzle-100) echo "otel-muzzle, ratio 100%|muzzle|1|0" ;;
    header-10)  echo "otel-muzzle, ratio 0%, 10% of requests force verbose|muzzle|0|0.1" ;;
    *) echo "unknown variant: $1" >&2; exit 2 ;;
  esac
}

# Read one KEY=value from a dotenv file without sourcing it (values may be quoted).
env_value() {
  local key="$1" file="$2"
  sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$file" | tail -n 1 \
    | sed -E 's/^"(.*)"[[:space:]]*$/\1/; s/^'"'"'(.*)'"'"'[[:space:]]*$/\1/'
}

# --- Span forwarding (optional) --------------------------------------------------------------
COLLECTOR_OVERLAY=local-only
FORWARD_EXPORT=false
FORWARD_PREFLIGHT=""
if [[ "$LOCAL_ONLY" != "1" && -f ../.env ]]; then
  OTLP_FORWARD_ENDPOINT="$(env_value OTLP_FORWARD_ENDPOINT ../.env)"
  OTLP_FORWARD_AUTHORIZATION="$(env_value OTLP_FORWARD_AUTHORIZATION ../.env)"
  if [[ -n "$OTLP_FORWARD_ENDPOINT" && -n "$OTLP_FORWARD_AUTHORIZATION" ]]; then
    # One empty OTLP request first, so rejected credentials fall back to local-only instead of
    # an export error per batch. The header reaches curl through a file descriptor, so the key
    # never appears on its command line.
    FORWARD_PREFLIGHT="$(curl -s -o /dev/null -m 15 -w '%{http_code}' -X POST \
      -H @<(printf 'Authorization: %s\n' "$OTLP_FORWARD_AUTHORIZATION") -H 'Content-Type: application/json' \
      --data '{"resourceSpans":[]}' "${OTLP_FORWARD_ENDPOINT%/}/v1/traces" 2>/dev/null || true)"
    if [[ "$FORWARD_PREFLIGHT" == 2* ]]; then
      export OTLP_FORWARD_ENDPOINT OTLP_FORWARD_AUTHORIZATION
      COLLECTOR_OVERLAY=forward
      FORWARD_EXPORT=true
    else
      FORWARD_REJECTED="The forwarding endpoint did not accept the credentials in ../.env (preflight HTTP ${FORWARD_PREFLIGHT:-no response}); check OTLP_FORWARD_AUTHORIZATION and OTLP_FORWARD_ENDPOINT."
    fi
  fi
fi
export COLLECTOR_OVERLAY
if [[ "$FORWARD_EXPORT" == true ]]; then
  log "Span forwarding: ON. Spans also go to OTLP_FORWARD_ENDPOINT from ../.env as environment muzzle-bench-<variant>. Set LOCAL_ONLY=1 to keep everything local."
elif [[ -n "${FORWARD_REJECTED:-}" ]]; then
  log "WARNING: ${FORWARD_REJECTED} Continuing with everything local."
else
  log "Span forwarding: off. Everything stays on this machine."
fi

# --- Images ---------------------------------------------------------------------------------
if [[ "$SKIP_BUILD" != "1" ]]; then
  for app in "${APPS[@]}"; do
    log "building ${BENCH_PREFIX}/${app}:${DEMO_VERSION} (stock ${STOCK_PREFIX}-${app} + otel-muzzle)"
    docker build -q -t "${BENCH_PREFIX}/${app}:${DEMO_VERSION}" -f "images/${app}/Dockerfile" .. >/dev/null
  done
fi

mkdir -p "$RUN_DIR/_setup"
compose down --remove-orphans >/dev/null 2>&1 || true

log "starting shared services (collector, flagd, astronomy-db, product-catalog)"
BENCH_VARIANT=_setup BENCH_PHASE=setup compose up -d --wait otel-collector flagd astronomy-db product-catalog

variants_json=""
for name in $VARIANTS; do
  IFS='|' read -r label images ratio header_ratio <<<"$(variant_def "$name")"
  mkdir -p "$RUN_DIR/$name"
  export BENCH_VARIANT="$name" OTEL_MUZZLE_VERBOSE_RATIO="$ratio"
  for app in "${APPS[@]}"; do
    var="$(echo "$app" | tr '[:lower:]' '[:upper:]')_IMAGE"
    if [[ "$images" == stock ]]; then
      export "$var=${STOCK_PREFIX}-${app}"
    else
      export "$var=${BENCH_PREFIX}/${app}:${DEMO_VERSION}"
    fi
  done

  log "variant ${name}: ${label}"
  BENCH_PHASE=warmup compose up -d --wait --force-recreate otel-collector "${APPS[@]}"
  compose run --rm -e RATE="$WARMUP_RATE" -e DURATION="$WARMUP_DURATION" -e HEADER_RATIO="$header_ratio" \
    -e SUMMARY_PATH="/out/${name}/k6-warmup.json" k6 run --quiet load/k6.js >/dev/null
  sleep "$FLUSH_WAIT"   # let warmup spans leave the SDK queues before measuring

  BENCH_PHASE=measure compose up -d --wait --force-recreate otel-collector
  compose run --rm -e RATE="$RATE" -e DURATION="$DURATION" -e HEADER_RATIO="$header_ratio" \
    -e SUMMARY_PATH="/out/${name}/k6-measure.json" k6 run --quiet load/k6.js
  sleep "$FLUSH_WAIT"
  compose stop "${APPS[@]}" >/dev/null       # graceful shutdown flushes whatever is left
  BENCH_PHASE=measure compose stop otel-collector >/dev/null
  compose logs --no-color otel-collector >"$RUN_DIR/$name/collector.log" 2>&1
  compose logs --no-color "${APPS[@]}" >"$RUN_DIR/$name/apps.log" 2>&1
  if [[ "$FORWARD_EXPORT" == true ]] && grep -qiE 'error|dropping' "$RUN_DIR/$name/collector.log"; then
    log "WARNING: the collector logged export errors for ${name}; see ${RUN_DIR}/${name}/collector.log"
  fi

  images_json=""
  for app in "${APPS[@]}"; do
    var="$(echo "$app" | tr '[:lower:]' '[:upper:]')_IMAGE"
    id="$(docker image inspect --format '{{.Id}}' "${!var}")"
    images_json+="${images_json:+, }\"${app}\": {\"image\": \"${!var}\", \"id\": \"${id}\"}"
  done
  variants_json+="${variants_json:+,
    }{\"name\": \"${name}\", \"label\": \"${label}\", \"images\": \"${images}\", \"ratio\": \"${ratio}\", \"header_ratio\": ${header_ratio}, \"image_ids\": {${images_json}}}"
done

compose down --remove-orphans >/dev/null

cat >"$RUN_DIR/run.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "demo_version": "${DEMO_VERSION}",
  "rate": ${RATE},
  "duration": "${DURATION}",
  "warmup_rate": ${WARMUP_RATE},
  "warmup_duration": "${WARMUP_DURATION}",
  "project_rps": ${PROJECT_RPS},
  "forward_export": ${FORWARD_EXPORT},
  "forward_preflight_http": "${FORWARD_PREFLIGHT}",
  "variants": [
    ${variants_json}
  ]
}
EOF

log "analysing ${RUN_DIR}"
docker run --rm -u "${BENCH_UID}:${BENCH_GID}" -v "$PWD:/bench" -w /bench python:3.12-slim \
  python analyze.py "$RUN_DIR" >/dev/null
cp "$RUN_DIR/results.md" RESULTS.md
cat "$RUN_DIR/results.md"
log "done: ${RUN_DIR}/results.{md,csv,json}; latest copied to bench/RESULTS.md"
