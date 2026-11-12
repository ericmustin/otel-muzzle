// SPDX-License-Identifier: Apache-2.0

import type { Context, Span, SpanOptions, Tracer } from '@opentelemetry/api';

/** The key under which a patched `startSpan` keeps the SDK's own. `Symbol.for`, so a second copy of this package sees the patch too. */
export const ORIGINAL_START_SPAN: unique symbol = Symbol.for(
  'otel-muzzle.original-startSpan'
);

/** Marks a MuzzleSampler from any copy of this package, so a second copy recognizes it. */
export const MUZZLE_SAMPLER: unique symbol = Symbol.for(
  'otel-muzzle.MuzzleSampler'
);

export type StartSpan = (
  this: Tracer,
  name: string,
  options?: SpanOptions,
  ctx?: Context
) => Span;
