// SPDX-License-Identifier: Apache-2.0

import * as path from 'node:path';
import {
  context,
  createTraceState,
  diag,
  DiagLogLevel,
  ROOT_CONTEXT,
  SpanKind,
  trace,
  TraceFlags,
  type Context,
  type Span,
  type SpanOptions,
  type TraceState,
  type Tracer,
} from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import {
  AlwaysOnSampler,
  InMemorySpanExporter,
  ParentBasedSampler,
  SimpleSpanProcessor,
  TracerProvider,
  type ReadableSpan,
  type Sampler,
} from '@opentelemetry/sdk-trace';
import { ORIGINAL_START_SPAN } from '../src/internal-types';
import { MuzzleSampler } from '../src/muzzle-sampler';

/** Trace-id bytes 12-15 are 0xffffffff: never verbose by ratio unless the ratio is 1. */
export const QUIET_TRACE_ID = '4bf92f3577b34da6a3ce929dffffffff';
/** Trace-id bytes 12-15 are 0: verbose by ratio whenever the ratio is above 0. */
export const LOUD_TRACE_ID = '4bf92f3577b34da6a3ce929d00000000';
export const REMOTE_SPAN_ID = '00f067aa0ba902b7';
export const LOCAL_SPAN_ID = '53995c3f42cd8ad8';

/** The package directory (js/). */
export const PACKAGE_DIR = path.resolve(__dirname, '..');
export const RATIO_VECTORS = path.resolve(
  PACKAGE_DIR,
  '../testdata/ratio_vectors.json'
);
export const RATIO_ENV_VECTORS = path.resolve(
  PACKAGE_DIR,
  '../testdata/ratio_env_vectors.json'
);

/** Registers an AsyncLocalStorage context manager for every test in the calling `describe`. */
export function useContextManager(): void {
  before(() => {
    context.setGlobalContextManager(
      new AsyncLocalStorageContextManager().enable()
    );
  });
  after(() => {
    context.disable();
  });
}

/** An SDK provider that exports synchronously into `exporter`. */
export function sdkProvider(
  exporter: InMemorySpanExporter,
  sampler?: Sampler
): TracerProvider {
  return new TracerProvider({
    sampler,
    spanProcessors: [new SimpleSpanProcessor({ exporter })],
  });
}

/** Finished spans by name. */
export function exported(
  exporter: InMemorySpanExporter
): Record<string, ReadableSpan> {
  return Object.fromEntries(
    exporter.getFinishedSpans().map(span => [span.name, span])
  );
}

/** Sorted names of the finished spans. */
export function names(exporter: InMemorySpanExporter): string[] {
  return exporter
    .getFinishedSpans()
    .map(span => span.name)
    .sort();
}

/** The parent span id of a finished span (the SDK 2.x and 3.x `ReadableSpan` agree on `parentSpanContext`). */
export function parentId(span: ReadableSpan): string | undefined {
  return span.parentSpanContext?.spanId;
}

/** `startActiveSpan` that always ends the span. `parent` defaults to the active context. */
export function inSpan<T>(
  tracer: Tracer,
  name: string,
  options: SpanOptions,
  fn: (span: Span) => T,
  parent: Context = context.active()
): T {
  return tracer.startActiveSpan(name, options, parent, span => {
    try {
      return fn(span);
    } finally {
      span.end();
    }
  });
}

/** SERVER -> INTERNAL "work" -> CLIENT, under `parent`. Returns the three spans. */
export function serverWorkClient(
  tracer: Tracer,
  parent: Context = ROOT_CONTEXT
): Span[] {
  return inSpan(
    tracer,
    'GET /',
    { kind: SpanKind.SERVER },
    server =>
      inSpan(tracer, 'work', {}, work =>
        inSpan(tracer, 'GET upstream', { kind: SpanKind.CLIENT }, client => [
          server,
          work,
          client,
        ])
      ),
    parent
  );
}

/** A MuzzleSampler at ratio 0 over `delegate`, by default ParentBased(root: AlwaysOn). */
export function parentBasedMuzzle(
  delegate: Sampler = new ParentBasedSampler({ root: new AlwaysOnSampler() })
): MuzzleSampler {
  return new MuzzleSampler({ ratio: 0, delegate });
}

/** Undoes `patchTracerPrototype`: restores the `startSpan` that the patch wrapped. */
export function unpatchTracerPrototype(proto: {
  startSpan: Tracer['startSpan'];
}): void {
  const original = (
    proto.startSpan as { [ORIGINAL_START_SPAN]?: Tracer['startSpan'] }
  )[ORIGINAL_START_SPAN];
  if (original !== undefined) proto.startSpan = original;
}

interface ParentOptions {
  traceId?: string;
  sampled?: boolean;
  traceState?: TraceState;
}

/** A context whose current span is a remote parent, as a W3C extract would produce. */
export function remoteParent({
  traceId = QUIET_TRACE_ID,
  sampled = true,
  traceState,
}: ParentOptions = {}): Context {
  return trace.setSpanContext(ROOT_CONTEXT, {
    traceId,
    spanId: REMOTE_SPAN_ID,
    traceFlags: sampled ? TraceFlags.SAMPLED : TraceFlags.NONE,
    isRemote: true,
    traceState,
  });
}

/** A context whose current span is a local span with the given context. */
export function localParent({
  traceId = QUIET_TRACE_ID,
  sampled = true,
  traceState,
}: ParentOptions = {}): Context {
  return trace.setSpanContext(ROOT_CONTEXT, {
    traceId,
    spanId: LOCAL_SPAN_ID,
    traceFlags: sampled ? TraceFlags.SAMPLED : TraceFlags.NONE,
    traceState,
  });
}

export function verboseTraceState(): TraceState {
  return createTraceState('muzzle=verbose');
}

/** Routes `diag` messages at `level` and above into an array until `restore()`. */
export function captureDiag(level = DiagLogLevel.WARN): {
  messages: string[];
  restore: () => void;
} {
  const messages: string[] = [];
  const record =
    (prefix: string) =>
    (...args: unknown[]) => {
      const message = `${prefix} ${args.join(' ')}`;
      // The API logs its own (un)registration of this logger at DEBUG.
      if (
        !/^\w+ @opentelemetry\/api: (Registered|Unregistering) a global for diag/.test(
          message
        )
      )
        messages.push(message);
    };
  diag.setLogger(
    {
      error: record('ERROR'),
      warn: record('WARN'),
      info: record('INFO'),
      debug: record('DEBUG'),
      verbose: record('VERBOSE'),
    },
    { logLevel: level, suppressOverrideMessage: true }
  );
  return { messages, restore: () => diag.disable() };
}

/** Runs `fn` with `diag` captured; returns its result and the messages logged. */
export function withDiag<T>(
  fn: () => T,
  level = DiagLogLevel.WARN
): [T, string[]] {
  const diagnostics = captureDiag(level);
  try {
    return [fn(), diagnostics.messages];
  } finally {
    diagnostics.restore();
  }
}

/** Runs `fn` with the given environment variables set (undefined unsets), restoring them afterwards. */
export function withEnv<T>(
  vars: Record<string, string | undefined>,
  fn: () => T
): T {
  const saved = Object.fromEntries(
    Object.keys(vars).map(name => [name, process.env[name]])
  );
  const apply = (values: Record<string, string | undefined>) => {
    for (const [name, value] of Object.entries(values)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  };
  apply(vars);
  try {
    return fn();
  } finally {
    apply(saved);
  }
}
