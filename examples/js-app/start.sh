#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# code: app.js requires ./tracing first. zerocode: load the standard setup file, ./instrumentation.js, with --require
# (a flag rather than NODE_OPTIONS, so the healthcheck's `node -e` stays uninstrumented). operator: NODE_OPTIONS already
# carries the Operator's `--require /otel-auto-instrumentation-nodejs/autoinstrumentation.js`.
set -e
if [ "$OTEL_MODE" = zerocode ]; then
  # ZEROCODE_REGISTER: examples/compose/muzzle-off.yml sets the stock entry point,
  # @opentelemetry/auto-instrumentations-node/register, for a comparison without otel-muzzle.
  exec node --require "${ZEROCODE_REGISTER:-./instrumentation.js}" app.js
fi
exec node app.js
