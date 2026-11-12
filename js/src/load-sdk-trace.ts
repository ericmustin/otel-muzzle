// SPDX-License-Identifier: Apache-2.0

/**
 * Loads the application's `@opentelemetry/sdk-trace`, an optional peer without a version range, so that a missing
 * copy is named instead of reported as a bare "Cannot find module".
 */
import type * as SdkTrace from '@opentelemetry/sdk-trace';

const NAME = '@opentelemetry/sdk-trace';

function load(): typeof SdkTrace {
  try {
    require.resolve(NAME);
  } catch (cause) {
    throw new Error(
      `otel-muzzle needs ${NAME}; upgrade the OpenTelemetry JS SDK to 2.9 or later, or install ${NAME} at your SDK's version`,
      { cause }
    );
  }
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- optional peer, loaded on first use
  return require(NAME) as typeof SdkTrace;
}

export const {
  AlwaysOffSampler,
  AlwaysOnSampler,
  ParentBasedSampler,
  SamplingDecision,
  TraceIdRatioBasedSampler,
} = load();
