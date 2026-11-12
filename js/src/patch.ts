// SPDX-License-Identifier: Apache-2.0

import {
  context,
  createContextKey,
  isSpanContextValid,
  trace,
  TraceFlags,
  type Context,
  type Span,
  type SpanOptions,
  type Tracer,
} from '@opentelemetry/api';
import { ORIGINAL_START_SPAN, type StartSpan } from './internal-types';

/**
 * The context key `suppressTracing()` sets in `@opentelemetry/core`, which the SDK checks with
 * `isTracingSuppressed()`. `createContextKey` is `Symbol.for`, so every copy of core and of this package agrees on
 * it without otel-muzzle depending on core.
 */
const SUPPRESS_TRACING_KEY = createContextKey(
  'OpenTelemetry SDK Context Key SUPPRESS_TRACING'
);

type PatchedStartSpan = StartSpan & { [ORIGINAL_START_SPAN]?: StartSpan };

/**
 * Wraps the SDK's `startSpan`. It holds no policy: when the SDK hands back a non-recording span whose parent is
 * valid, local and sampled, it returns a span with the parent's own span context instead, so the dropped span's
 * children attach to the nearest kept ancestor. Everything else (spans under an unsampled or remote parent,
 * roots, suppressed spans, and every recording span) is returned exactly as the SDK built it.
 */
export function wrapStartSpan(original: StartSpan): StartSpan {
  const startSpan: PatchedStartSpan = function startSpan(
    this: Tracer,
    name: string,
    options: SpanOptions = {},
    ctx: Context = context.active()
  ): Span {
    const span = original.call(this, name, options, ctx);
    if (span.isRecording()) return span;
    // The parent context the SDK used: for a requested root, it removes the span from the context.
    const parentContext = options.root ? trace.deleteSpan(ctx) : ctx;
    // Under suppressed tracing, the SDK returns an invalid span on purpose.
    if (parentContext.getValue(SUPPRESS_TRACING_KEY) === true) return span;
    const parent = trace.getSpanContext(parentContext);
    if (
      parent === undefined ||
      !isSpanContextValid(parent) ||
      parent.isRemote === true ||
      (parent.traceFlags & TraceFlags.SAMPLED) === 0
    ) {
      return span;
    }
    // The parent's own context: trace id, span id, flags and tracestate.
    return trace.wrapSpanContext(parent);
  };
  Object.defineProperty(startSpan, ORIGINAL_START_SPAN, { value: original });
  return startSpan;
}

/** Returns true when `startSpan` is already wrapped, by this or any other copy of otel-muzzle. */
export function isPatched(startSpan: unknown): boolean {
  return (
    typeof startSpan === 'function' &&
    (startSpan as PatchedStartSpan)[ORIGINAL_START_SPAN] !== undefined
  );
}

/**
 * Wraps `startSpan` on the SDK's `Tracer` prototype, which reaches every tracer of that SDK copy, including ones
 * created earlier. `startActiveSpan` calls `this.startSpan`, so it goes through the wrapper too. Returns false when
 * the prototype is already patched.
 *
 * @param proto The SDK `Tracer` prototype.
 */
export function patchTracerPrototype(proto: { startSpan: StartSpan }): boolean {
  if (isPatched(proto.startSpan)) return false;
  proto.startSpan = wrapStartSpan(proto.startSpan);
  return true;
}
