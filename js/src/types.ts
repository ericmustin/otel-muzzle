// SPDX-License-Identifier: Apache-2.0

import type { TracerProvider } from '@opentelemetry/api';
import type { Sampler } from '@opentelemetry/sdk-trace';

export interface MuzzleSamplerOptions {
  /**
   * The sampler that decides first; its DROP always wins. Defaults to the sampler `OTEL_TRACES_SAMPLER` selects,
   * or `ParentBased(root: AlwaysOn)` when it is unset, which is what NodeSDK would use without otel-muzzle.
   */
  delegate?: Sampler;
  /** Share of traces that keep every span, in [0, 1]. Defaults to `OTEL_MUZZLE_VERBOSE_RATIO`, else 0.1. */
  ratio?: number;
}

export interface InstallMuzzleOptions {
  /** Defaults to the global provider, unwrapped from the API's proxy. */
  tracerProvider?: TracerProvider;
}
