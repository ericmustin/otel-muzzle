// SPDX-License-Identifier: Apache-2.0

import type { TracerProvider } from '@opentelemetry/api';
import type { Sampler } from '@opentelemetry/sdk-trace';
import type { SpanSamplingImplementer } from './policy/span-sampling';

export interface MuzzleSamplerOptions {
  /**
   * The sampler that decides first; its DROP always wins. Defaults to the sampler `OTEL_TRACES_SAMPLER` selects,
   * or `ParentBased(root: AlwaysOn)` when it is unset, which is what NodeSDK would use without otel-muzzle.
   */
  delegate?: Sampler;
  /**
   * Share of traces that keep every span, in [0, 1]. Defaults to `OTEL_MUZZLE_VERBOSE_RATIO`, else 0.1. In policy
   * mode it is checked, then ignored with a warning.
   */
  ratio?: number;
  /**
   * A file of span sampling policies, in Protobuf JSON. With one, the sampler is in policy mode: the policies
   * decide instead of the built-in rule. Defaults to `OTEL_MUZZLE_POLICY_FILE`. A file that cannot be read leaves
   * no policy in effect, so nothing is dropped. Experimental.
   */
  policyFile?: string;
  /** The span sampling policies to decide with, instead of a policy file. Experimental. */
  policies?: SpanSamplingImplementer;
}

export interface InstallMuzzleOptions {
  /** Defaults to the global provider, unwrapped from the API's proxy. */
  tracerProvider?: TracerProvider;
}
