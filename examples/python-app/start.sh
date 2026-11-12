#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# zerocode: run under opentelemetry-instrument. operator: PYTHONPATH (set by the Operator, or by
# examples/compose/operator.yml) puts the Operator's sitecustomize first, which does the same work at startup.
# code: app.py sets up the SDK itself.
set -e
if [ "$OTEL_MODE" = zerocode ]; then
  exec opentelemetry-instrument python app.py
fi
exec python app.py
