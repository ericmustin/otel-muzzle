// SPDX-License-Identifier: Apache-2.0
'use strict';

const { NodeSDK } = require('@opentelemetry/sdk-node');
const {
  getNodeAutoInstrumentations,
  getResourceDetectors,
} = require('@opentelemetry/auto-instrumentations-node');
const { MuzzleSampler, installMuzzle } = require('otel-muzzle');

const sdk = new NodeSDK({
  sampler: new MuzzleSampler(),
  instrumentations: getNodeAutoInstrumentations(),
  resourceDetectors: getResourceDetectors(),
});
sdk.start();
installMuzzle();

process.on('SIGTERM', () => {
  sdk.shutdown().finally(() => process.exit(0));
});
