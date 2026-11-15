#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Runs the otel-muzzle benchmark on GitLab CE: the same k6 workload against GitLab with its own OpenTelemetry
# tracing, then with otel-muzzle added, and reports spans and OTLP bytes per request. See bench/gitlab/README.md.
# Everything runs in local Docker; no span leaves this machine.
#
# Knobs (environment variables):
#   RATE=4                 k6 iterations per second during the measured phase
#   DURATION=600s          length of the measured phase, per variant
#   WARMUP_RATE=2          iterations per second during the (discarded) warmup phase
#   WARMUP_DURATION=60s
#   VARIANTS="baseline muzzle-0 muzzle-10 header-10 baseline-2"   needs two stock variants
#   SKIP_BUILD=0           1 = reuse the muzzle-bench/gitlab images already built
#   FLUSH_WAIT=10          seconds to wait after each phase for the exporters to flush
#   RUN_ID=<UTC timestamp> names the run directory, out/gitlab/<RUN_ID>
set -euo pipefail
cd "$(dirname "$0")"

RATE="${RATE:-4}"
DURATION="${DURATION:-600s}"
WARMUP_RATE="${WARMUP_RATE:-2}"
WARMUP_DURATION="${WARMUP_DURATION:-60s}"
VARIANTS="${VARIANTS:-baseline muzzle-0 muzzle-10 header-10 baseline-2}"
SKIP_BUILD="${SKIP_BUILD:-0}"
FLUSH_WAIT="${FLUSH_WAIT:-10}"   # seconds; longer than the Ruby SDK's 5 s batch delay
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

GITLAB_VERSION=19.4.5-ce.0
SEED_REPO=https://github.com/sinatra/sinatra.git
SEED_TAG=v4.2.1

export BENCH_UID="$(id -u)" BENCH_GID="$(id -g)"
export BENCH_OUT="../out/gitlab/${RUN_ID}"
export BENCH_TOKEN="${BENCH_TOKEN:-muzzle-bench-api-token}"   # compose.yaml has the same default
RUN_DIR="../out/gitlab/${RUN_ID}"

log() { printf '\n[bench-gitlab %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
# k6_run SCRIPT [docker compose run options]
k6_run() { local script="$1"; shift; docker compose --profile load run --rm "$@" k6 run --quiet "/bench/${script}"; }

# The readiness check can pass before every Puma worker serves: wait until the API answers for the seeded project.
api_ready() {
  for _ in $(seq 60); do
    docker compose exec -T gitlab curl -fs -o /dev/null -H "PRIVATE-TOKEN: ${BENCH_TOKEN}" \
      "http://localhost/api/v4/projects/root%2Fsinatra" && return 0
    sleep 5
  done
  log "GitLab's API did not answer within 5 minutes"
  return 1
}

# name -> "label|image|ratio|header_ratio"
variant_def() {
  case "$1" in
    baseline)   echo "GitLab's own tracing (baseline)|stock|0|0" ;;
    muzzle-0)   echo "otel-muzzle, ratio 0%|muzzle|0|0" ;;
    muzzle-10)  echo "otel-muzzle, ratio 10% (default)|muzzle|0.1|0" ;;
    header-10)  echo "otel-muzzle, ratio 0%, 10% of requests force verbose|muzzle|0|0.1" ;;
    baseline-2) echo "GitLab's own tracing, second run|stock|0|0" ;;
    *) echo "unknown variant: $1" >&2; exit 2 ;;
  esac
}

if [[ "$SKIP_BUILD" != "1" ]]; then
  for target in stock muzzle; do
    log "building muzzle-bench/gitlab:${target}"
    docker build -q --target "$target" -t "muzzle-bench/gitlab:${target}" -f Dockerfile ../.. >/dev/null
  done
fi

mkdir -p "$RUN_DIR/_setup"
export BENCH_VARIANT=_setup BENCH_PHASE=setup GITLAB_IMAGE=muzzle-bench/gitlab:stock OTEL_MUZZLE_VERBOSE_RATIO=0
log "starting GitLab ${GITLAB_VERSION} (a first boot takes several minutes)"
docker compose up -d --wait otel-collector gitlab
log "seeding root/sinatra from Sinatra ${SEED_TAG}"
docker compose exec -T -e BENCH_TOKEN -e OTEL_SDK_DISABLED=true gitlab gitlab-rails runner /bench/setup.rb   # untraced
sleep 60   # each GitLab process caches application settings for 60 s; the push below must see Auto DevOps off
docker compose exec -T -e BENCH_TOKEN gitlab sh -c "rm -rf /tmp/seed && git clone -q --branch ${SEED_TAG} ${SEED_REPO} /tmp/seed \
  && git -C /tmp/seed push -q \"http://root:\${BENCH_TOKEN}@localhost/root/sinatra.git\" HEAD:refs/heads/main"
k6_run seed.js

variants_json=""
for name in $VARIANTS; do
  IFS='|' read -r label image ratio header_ratio <<<"$(variant_def "$name")"
  mkdir -p "$RUN_DIR/$name"
  export BENCH_VARIANT="$name" GITLAB_IMAGE="muzzle-bench/gitlab:${image}" OTEL_MUZZLE_VERBOSE_RATIO="$ratio"

  log "variant ${name}: ${label}"
  BENCH_PHASE=warmup docker compose up -d --wait --force-recreate otel-collector gitlab
  api_ready
  k6_run k6.js -e RATE="$WARMUP_RATE" -e DURATION="$WARMUP_DURATION" -e HEADER_RATIO="$header_ratio" \
    -e SUMMARY_PATH="/out/${name}/k6-warmup.json" >/dev/null
  sleep "$FLUSH_WAIT"   # let warmup spans leave the SDK queues before measuring

  # GitLab keeps running: its background work between the two phases belongs to the measured window too.
  BENCH_PHASE=measure docker compose up -d --wait --force-recreate otel-collector
  k6_run k6.js -e RATE="$RATE" -e DURATION="$DURATION" -e HEADER_RATIO="$header_ratio" \
    -e SUMMARY_PATH="/out/${name}/k6-measure.json"
  sleep "$FLUSH_WAIT"
  docker compose stop otel-collector >/dev/null
  docker compose logs --no-color otel-collector >"$RUN_DIR/$name/collector.log" 2>&1
  docker compose logs --no-color --tail 200 gitlab >"$RUN_DIR/$name/gitlab.log" 2>&1

  image_id="$(docker image inspect --format '{{.Id}}' "$GITLAB_IMAGE")"
  variants_json+="${variants_json:+,
    }{\"name\": \"${name}\", \"label\": \"${label}\", \"images\": \"${image}\", \"ratio\": \"${ratio}\", \"header_ratio\": ${header_ratio}, \"image_id\": \"${image_id}\"}"
done

docker compose down --remove-orphans >/dev/null   # keeps the volumes, so the next run skips the seeding work

cat >"$RUN_DIR/run.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "gitlab_version": "${GITLAB_VERSION}",
  "rate": ${RATE},
  "duration": "${DURATION}",
  "warmup_rate": ${WARMUP_RATE},
  "warmup_duration": "${WARMUP_DURATION}",
  "variants": [
    ${variants_json}
  ]
}
EOF

log "analysing ${RUN_DIR}"
status=0
docker run --rm -u "${BENCH_UID}:${BENCH_GID}" -v "$PWD/..:/bench" -w /bench python:3.12-slim \
  python analyze_app.py "out/gitlab/${RUN_ID}" >/dev/null || status=$?
cp "$RUN_DIR/results.md" RESULTS.md
cat "$RUN_DIR/results.md"
if [[ "$status" != 0 ]]; then
  log "some checks failed: see ${RUN_DIR}/results.md"
fi
log "done: ${RUN_DIR}/results.{md,json}; latest copied to bench/gitlab/RESULTS.md"
exit "$status"
