# A custom spec.nodejs.image for the OpenTelemetry Operator: what the Operator's autoinstrumentation-nodejs image
# holds (autoinstrumentation/nodejs in open-telemetry/opentelemetry-operator: @opentelemetry/api 1.10.0 and
# auto-instrumentations-node 0.82.0, under /autoinstrumentation), plus otel-muzzle, with an autoinstrumentation.js
# that starts a NodeSDK with a MuzzleSampler and calls installMuzzle().
#
# The Operator copies it into the pod with an init container running
#   cp -r /autoinstrumentation/. /otel-auto-instrumentation-nodejs
# into a shared volume mounted at /otel-auto-instrumentation-nodejs, and sets on the app container
#   NODE_OPTIONS=" --require /otel-auto-instrumentation-nodejs/autoinstrumentation.js"
# (internal/instrumentation/nodejs.go).
#
# Build from the repository root, which holds the otel-muzzle source:
#   docker build -f examples/operator/nodejs/Dockerfile -t autoinstrumentation-nodejs-muzzle .
FROM node:22-slim AS build

# otel-muzzle as npm would publish it: npm pack runs prepack, which builds it.
COPY js /pkg/js
RUN cd /pkg/js && npm ci --no-audit --no-fund && npm pack --pack-destination /pkg && mv /pkg/otel-muzzle-*.tgz /pkg/otel-muzzle.tgz

WORKDIR /autoinstrumentation
COPY examples/operator/nodejs/package.json examples/operator/nodejs/autoinstrumentation.js ./
RUN npm install --omit=dev --no-audit --no-fund --no-package-lock

# As the Operator's image: busybox, so the init container has cp.
FROM busybox:1.38.0
COPY --from=build /autoinstrumentation /autoinstrumentation
RUN chmod -R go+r /autoinstrumentation
