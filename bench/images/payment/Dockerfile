# OpenTelemetry Demo 3.1.0 payment service (Node.js, gRPC) plus otel-muzzle.
# Build context: the repository root (so js/ can be copied in).
# The stock image starts @opentelemetry/auto-instrumentations-node's `register` (0.78.0) through NODE_OPTIONS. This
# image swaps in examples/js-app/instrumentation.js, the standard setup file: it starts a NodeSDK with the same
# instrumentations plus otel-muzzle. It is installed next to the versions the demo's agent ships, so nothing else
# changes.
ARG DEMO_IMAGE=ghcr.io/open-telemetry/demo:3.1.0-payment

FROM node:22-slim AS build
WORKDIR /src
COPY js ./
RUN npm ci --no-audit --no-fund && npm pack --pack-destination /tmp
WORKDIR /otel-muzzle
RUN npm init -y >/dev/null \
 && npm install --omit=dev --no-audit --no-fund /tmp/otel-muzzle-*.tgz \
      @opentelemetry/api@1.9.1 @opentelemetry/sdk-trace@2.9.0 @opentelemetry/sdk-node@0.220.0 \
      @opentelemetry/auto-instrumentations-node@0.78.0
COPY examples/js-app/instrumentation.js /otel-muzzle/autoinstrumentation.js

FROM ${DEMO_IMAGE}
COPY --from=build /otel-muzzle /otel-muzzle
ENV NODE_OPTIONS="--require /otel-muzzle/autoinstrumentation.js"
