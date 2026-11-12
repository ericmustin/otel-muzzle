# OpenTelemetry Demo 3.1.0 recommendation service (Python, opentelemetry-instrument) plus otel-muzzle.
# Build context: the repository root (so python/ can be copied in).
# The demo already runs under opentelemetry-instrument, so installing the package is the whole change: its
# `muzzle` instrumentor entry point loads by itself. --no-deps keeps the app's pinned OpenTelemetry as it is.
ARG DEMO_IMAGE=ghcr.io/open-telemetry/demo:3.1.0-recommendation
FROM ${DEMO_IMAGE}
COPY python /tmp/otel-muzzle
RUN /venv/bin/pip install --quiet --no-cache-dir --no-deps /tmp/otel-muzzle \
 && rm -rf /tmp/otel-muzzle
