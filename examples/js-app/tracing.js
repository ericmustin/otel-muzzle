// SPDX-License-Identifier: Apache-2.0
'use strict';

// OTEL_MODE=code: the app configures the SDK. app.js requires this first, because instrumentations patch express,
// pg and http when those modules are required.
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { ExpressInstrumentation } = require('@opentelemetry/instrumentation-express');
const { HttpInstrumentation } = require('@opentelemetry/instrumentation-http');
const { PgInstrumentation } = require('@opentelemetry/instrumentation-pg');
const { MuzzleSampler, installMuzzle } = require('otel-muzzle');

const sdk = new NodeSDK({
  // Wraps the sampler OTEL_TRACES_SAMPLER selects. The ratio comes from OTEL_MUZZLE_VERBOSE_RATIO.
  sampler: new MuzzleSampler(),
  instrumentations: [
    new HttpInstrumentation({ ignoreIncomingRequestHook: (req) => req.url === '/health' }),
    new ExpressInstrumentation(),
    new PgInstrumentation(),
  ],
});
sdk.start();

// After start(), which is when NodeSDK registers its TracerProvider (OTEL_TRACES_EXPORTER gives it an exporter).
if (!installMuzzle()) throw new Error('installMuzzle() found no OpenTelemetry SDK TracerProvider');

process.on('SIGTERM', () => sdk.shutdown().finally(() => process.exit(0)));
