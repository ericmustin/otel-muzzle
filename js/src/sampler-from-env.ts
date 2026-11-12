// SPDX-License-Identifier: Apache-2.0

import type { Sampler } from '@opentelemetry/sdk-trace';
import {
  AlwaysOffSampler,
  AlwaysOnSampler,
  ParentBasedSampler,
  TraceIdRatioBasedSampler,
} from './load-sdk-trace';
import { logger } from './utils';

/**
 * Returns the sampler `OTEL_TRACES_SAMPLER` (with `OTEL_TRACES_SAMPLER_ARG`) selects, or undefined when the
 * variable is unset or blank. It also returns undefined, after a warning, for a name the SDK cannot build from
 * the environment (`jaeger_remote`, `parentbased_jaeger_remote` and `xray` need packages of their own). The name
 * is trimmed and case-insensitive, as the specification asks of enum values.
 *
 * @param env The environment to read. Defaults to `process.env`.
 * @param warn Receives each warning. Defaults to the component logger.
 */
export function samplerFromEnv(
  env: NodeJS.ProcessEnv = process.env,
  warn: (message: string) => void = message => logger.warn(message)
): Sampler | undefined {
  const raw = env.OTEL_TRACES_SAMPLER?.trim();
  if (!raw) return undefined;
  switch (raw.toLowerCase()) {
    case 'always_on':
      return new AlwaysOnSampler();
    case 'always_off':
      return new AlwaysOffSampler();
    case 'traceidratio':
      return new TraceIdRatioBasedSampler(samplerArgFromEnv(env, warn));
    case 'parentbased_always_on':
      return new ParentBasedSampler({ root: new AlwaysOnSampler() });
    case 'parentbased_always_off':
      return new ParentBasedSampler({ root: new AlwaysOffSampler() });
    case 'parentbased_traceidratio':
      return new ParentBasedSampler({
        root: new TraceIdRatioBasedSampler(samplerArgFromEnv(env, warn)),
      });
    default:
      warn(
        `unknown OTEL_TRACES_SAMPLER value ${JSON.stringify(raw)}, using the default sampler`
      );
      return undefined;
  }
}

/** Returns `OTEL_TRACES_SAMPLER_ARG`; a blank, non-numeric or out-of-range value is logged and replaced by 1. */
function samplerArgFromEnv(
  env: NodeJS.ProcessEnv,
  warn: (message: string) => void
): number {
  const raw = env.OTEL_TRACES_SAMPLER_ARG?.trim() ?? '';
  const ratio = raw === '' ? Number.NaN : Number(raw);
  if (ratio >= 0 && ratio <= 1) return ratio;
  warn(
    `OTEL_TRACES_SAMPLER_ARG must be a number within [0, 1], got ${JSON.stringify(raw)}; using 1`
  );
  return 1;
}
