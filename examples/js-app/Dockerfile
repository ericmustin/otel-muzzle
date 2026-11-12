# Build context: the repository root (see docker-compose.yml).
FROM node:22-slim

# code: tracing.js configures NodeSDK with a MuzzleSampler and calls installMuzzle(). zerocode: no OpenTelemetry
# code; start.sh runs `node --require ./instrumentation.js app.js`. operator: the image has neither the SDK nor
# otel-muzzle; NODE_OPTIONS loads both from the volume the Operator's init container fills from the custom image in
# examples/operator/nodejs.
ARG OTEL_MODE=code
ENV OTEL_MODE=${OTEL_MODE}

# The package under test, built from source.
COPY js /pkg/js
RUN if [ "$OTEL_MODE" != operator ]; then cd /pkg/js && npm ci --no-audit --no-fund && npm run compile; fi

WORKDIR /app
COPY examples/js-app/package.json ./
# --install-links copies /pkg/js in like a published package instead of symlinking it, so its peers
# (@opentelemetry/api, sdk-trace, sdk-node, auto-instrumentations-node) resolve to the app's copies.
RUN case "$OTEL_MODE" in \
      code) npm install --install-links --no-audit --no-fund /pkg/js @opentelemetry/sdk-node@0.301.0 \
              @opentelemetry/instrumentation-http@0.301.0 @opentelemetry/instrumentation-express@0.73.0 \
              @opentelemetry/instrumentation-pg@0.78.0 ;; \
      zerocode) npm install --install-links --no-audit --no-fund /pkg/js @opentelemetry/sdk-node@0.301.0 \
              @opentelemetry/auto-instrumentations-node@0.83.0 ;; \
      operator) npm install --no-audit --no-fund ;; \
      *) echo "unknown OTEL_MODE '$OTEL_MODE' (code, zerocode or operator)" >&2; exit 1 ;; \
    esac
COPY examples/js-app/app.js examples/js-app/tracing.js examples/js-app/instrumentation.js examples/js-app/start.sh ./

CMD ["./start.sh"]
