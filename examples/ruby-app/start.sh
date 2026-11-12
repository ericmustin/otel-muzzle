#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# zerocode: preload the zero-code loader the way its README says to under `bundle exec`.
# operator: RUBYOPT already carries the operator's `-r /otel-auto-instrumentation-ruby/src/autoinstrumentation`.
set -e
if [ "$OTEL_MODE" = zerocode ]; then
  RUBYOPT="-r $(gem which opentelemetry-auto-instrumentation) ${RUBYOPT:-}"
  export RUBYOPT
fi
exec bundle exec ruby app.rb
