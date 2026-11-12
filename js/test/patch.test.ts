// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import {
  context,
  createTraceState,
  defaultTextMapSetter,
  INVALID_SPAN_CONTEXT,
  isSpanContextValid,
  ROOT_CONTEXT,
  SpanKind,
  trace,
  TraceFlags,
  type Span,
  type Tracer,
} from '@opentelemetry/api';
import {
  suppressTracing,
  W3CTraceContextPropagator,
} from '@opentelemetry/core';
import {
  AlwaysOffSampler,
  AlwaysOnSampler,
  InMemorySpanExporter,
  ParentBasedSampler,
  SamplingDecision,
  type Sampler,
  type SamplingResult,
} from '@opentelemetry/sdk-trace';
import { isPatched, patchTracerPrototype, wrapStartSpan } from '../src/patch';
import { ORIGINAL_START_SPAN } from '../src/internal-types';
import {
  exported,
  inSpan,
  names,
  parentBasedMuzzle,
  parentId,
  QUIET_TRACE_ID,
  REMOTE_SPAN_ID,
  remoteParent,
  sdkProvider,
  serverWorkClient,
  useContextManager,
  verboseTraceState,
} from './helpers';

let exporter: InMemorySpanExporter;

/** Drops every span whose name starts with "drop", whatever its kind or parent. */
class DropByName implements Sampler {
  constructor(private readonly delegate: Sampler) {}
  shouldSample(...args: Parameters<Sampler['shouldSample']>): SamplingResult {
    if (args[2].startsWith('drop'))
      return { decision: SamplingDecision.NOT_RECORD };
    return this.delegate.shouldSample(...args);
  }
  toString(): string {
    return 'DropByName';
  }
}

const parentBased = () =>
  new ParentBasedSampler({ root: new AlwaysOnSampler() });

/** A tracer with the patch applied to it alone, so the SDK's Tracer prototype stays untouched in this file. */
function patchedTracer(sampler: Sampler = parentBasedMuzzle()): Tracer {
  const tracer = sdkProvider(exporter, sampler).getTracer('test');
  const instance = tracer as Tracer & { startSpan: Tracer['startSpan'] };
  instance.startSpan = wrapStartSpan(Object.getPrototypeOf(tracer).startSpan);
  return tracer;
}

/** The W3C headers a propagator writes for a request made under `span`. */
function injected(span: Span): Record<string, string> {
  const carrier: Record<string, string> = {};
  new W3CTraceContextPropagator().inject(
    trace.setSpan(ROOT_CONTEXT, span),
    carrier,
    defaultTextMapSetter
  );
  return carrier;
}

describe('wrapStartSpan', () => {
  useContextManager();
  beforeEach(() => {
    exporter = new InMemorySpanExporter();
  });

  describe('reparenting', () => {
    it('should attach the children of a dropped span to its parent', () => {
      const [server] = serverWorkClient(patchedTracer());
      assert.deepStrictEqual(names(exporter), ['GET /', 'GET upstream']);
      assert.strictEqual(
        parentId(exported(exporter)['GET upstream']),
        server.spanContext().spanId
      );
    });

    it('should chain nested drops up to the kept ancestor', () => {
      const tracer = patchedTracer();
      const server = inSpan(
        tracer,
        'GET /',
        { kind: SpanKind.SERVER },
        server => {
          inSpan(tracer, 'outer', {}, () =>
            inSpan(tracer, 'inner', {}, () =>
              inSpan(tracer, 'query', { kind: SpanKind.CLIENT }, () => {})
            )
          );
          return server;
        }
      );
      assert.deepStrictEqual(names(exporter), ['GET /', 'query']);
      assert.strictEqual(
        parentId(exported(exporter).query),
        server.spanContext().spanId
      );
    });

    it("should give a dropped span its parent's context, and propagate it", () => {
      const tracer = patchedTracer();
      const [server, work] = serverWorkClient(
        tracer,
        remoteParent({ traceState: createTraceState('vendor=x') })
      );
      const parent = server.spanContext();
      const dropped = work.spanContext();
      assert.strictEqual(work.isRecording(), false);
      assert.strictEqual(dropped.traceId, parent.traceId);
      assert.strictEqual(dropped.spanId, parent.spanId);
      assert.strictEqual(dropped.traceFlags, TraceFlags.SAMPLED);
      assert.strictEqual(dropped.traceState?.serialize(), 'vendor=x');
      assert.notStrictEqual(dropped.isRemote, true);
      assert.deepStrictEqual(injected(work), {
        traceparent: `00-${parent.traceId}-${parent.spanId}-01`,
        tracestate: 'vendor=x',
      });
    });

    it('should leave the parent recording when a dropped span ends, and discard its attributes', () => {
      const tracer = patchedTracer();
      inSpan(tracer, 'GET /', { kind: SpanKind.SERVER }, server => {
        const work = tracer.startSpan('work');
        work.setAttribute('ignored', true);
        work.addEvent('ignored');
        work.end();
        assert.strictEqual(server.isRecording(), true);
      });
      assert.deepStrictEqual(names(exporter), ['GET /']);
      assert.strictEqual(
        exported(exporter)['GET /'].attributes.ignored,
        undefined
      );
      assert.deepStrictEqual(exported(exporter)['GET /'].events, []);
    });

    it('should reparent when startSpan gets an explicit parent context', () => {
      const tracer = patchedTracer();
      const server = tracer.startSpan('GET /', { kind: SpanKind.SERVER });
      const work = tracer.startSpan(
        'work',
        {},
        trace.setSpan(context.active(), server)
      );
      const client = tracer.startSpan(
        'GET upstream',
        { kind: SpanKind.CLIENT },
        trace.setSpan(context.active(), work)
      );
      for (const span of [client, work, server]) span.end();
      assert.strictEqual(
        parentId(exported(exporter)['GET upstream']),
        server.spanContext().spanId
      );
    });

    it('should route startActiveSpan through the patch with 2, 3 and 4 arguments', () => {
      const tracer = patchedTracer();
      inSpan(tracer, 'GET /', { kind: SpanKind.SERVER }, server => {
        const serverId = server.spanContext().spanId;
        tracer.startActiveSpan('two', span => {
          assert.strictEqual(span.spanContext().spanId, serverId);
          assert.strictEqual(trace.getActiveSpan(), span);
          span.end();
        });
        tracer.startActiveSpan('three', {}, span => {
          assert.strictEqual(span.spanContext().spanId, serverId);
          span.end();
        });
        tracer.startActiveSpan('four', {}, context.active(), span => {
          assert.strictEqual(span.spanContext().spanId, serverId);
          span.end();
        });
      });
      assert.deepStrictEqual(names(exporter), ['GET /']);
    });
  });

  describe('left to the SDK', () => {
    /** The SDK's own dropped span: valid, a fresh span id, unsampled. */
    function assertSdkDrop(span: Span, notSpanId: string): void {
      const spanContext = span.spanContext();
      assert.strictEqual(span.isRecording(), false);
      assert.ok(isSpanContextValid(spanContext));
      assert.notStrictEqual(spanContext.spanId, notSpanId);
      assert.strictEqual(spanContext.traceFlags, TraceFlags.NONE);
    }

    it('should leave spans under an unsampled parent to the SDK', () => {
      const spans = serverWorkClient(
        patchedTracer(),
        remoteParent({ sampled: false })
      );
      for (const span of spans) assertSdkDrop(span, REMOTE_SPAN_ID);
      assert.strictEqual(
        new Set(spans.map(span => span.spanContext().spanId)).size,
        3
      );
      assert.deepStrictEqual(names(exporter), []);
    });

    it('should leave a span dropped under a remote parent to the SDK', () => {
      const tracer = patchedTracer(
        parentBasedMuzzle(new DropByName(new AlwaysOnSampler()))
      );
      const span = tracer.startSpan(
        'drop me',
        { kind: SpanKind.SERVER },
        remoteParent()
      );
      assertSdkDrop(span, REMOTE_SPAN_ID);
      assert.strictEqual(span.spanContext().traceId, QUIET_TRACE_ID);
    });

    it('should leave a dropped root to the SDK', () => {
      const span = patchedTracer(
        parentBasedMuzzle(new DropByName(new AlwaysOnSampler()))
      ).startSpan('drop me', { kind: SpanKind.SERVER });
      assertSdkDrop(span, '');
    });

    it('should not reparent a dropped options.root span onto the active span', () => {
      const tracer = patchedTracer(
        parentBasedMuzzle(new DropByName(new AlwaysOnSampler()))
      );
      inSpan(tracer, 'GET /', { kind: SpanKind.SERVER }, server => {
        const job = tracer.startSpan('drop job', { root: true });
        assertSdkDrop(job, server.spanContext().spanId);
        assert.notStrictEqual(
          job.spanContext().traceId,
          server.spanContext().traceId
        );
      });
    });

    it("should return the SDK's invalid span for a suppressed span", () => {
      const tracer = patchedTracer();
      inSpan(tracer, 'GET /', { kind: SpanKind.SERVER }, () => {
        for (const kind of [SpanKind.INTERNAL, SpanKind.CLIENT]) {
          const span = tracer.startSpan(
            'suppressed',
            { kind },
            suppressTracing(context.active())
          );
          assert.deepStrictEqual(span.spanContext(), INVALID_SPAN_CONTEXT);
          span.end();
        }
        context.with(suppressTracing(context.active()), () => {
          tracer.startActiveSpan('suppressed', span => {
            assert.deepStrictEqual(span.spanContext(), INVALID_SPAN_CONTEXT);
            span.end();
          });
        });
      });
      assert.deepStrictEqual(names(exporter), ['GET /']);
    });
  });

  describe('policy', () => {
    it('should start a new trace for a kept options.root span', () => {
      const tracer = patchedTracer();
      inSpan(tracer, 'GET /', { kind: SpanKind.SERVER }, server => {
        const job = tracer.startSpan('job', { root: true });
        assert.strictEqual(job.isRecording(), true);
        assert.notStrictEqual(
          job.spanContext().traceId,
          server.spanContext().traceId
        );
        job.end();
      });
      assert.strictEqual(parentId(exported(exporter).job), undefined);
    });

    it('should make a span that another sampler drops under a sampled parent transparent', () => {
      const tracer = patchedTracer(
        parentBasedMuzzle(new DropByName(parentBased()))
      );
      const server = inSpan(
        tracer,
        'GET /',
        { kind: SpanKind.SERVER },
        server => {
          inSpan(tracer, 'drop me', { kind: SpanKind.CLIENT }, () =>
            inSpan(tracer, 'SELECT', { kind: SpanKind.CLIENT }, () => {})
          );
          return server;
        }
      );
      assert.deepStrictEqual(names(exporter), ['GET /', 'SELECT']);
      assert.strictEqual(
        parentId(exported(exporter).SELECT),
        server.spanContext().spanId
      );
    });

    it('should apply the patch to every provider of the SDK', () => {
      const tracer = patchedTracer(new DropByName(parentBased()));
      const server = inSpan(
        tracer,
        'GET /',
        { kind: SpanKind.SERVER },
        server => {
          inSpan(tracer, 'drop me', { kind: SpanKind.CLIENT }, () =>
            inSpan(tracer, 'SELECT', { kind: SpanKind.CLIENT }, () => {})
          );
          return server;
        }
      );
      assert.deepStrictEqual(names(exporter), ['GET /', 'SELECT']);
      assert.strictEqual(
        parentId(exported(exporter).SELECT),
        server.spanContext().spanId
      );
    });

    it('should keep every span of a verbose trace', () => {
      const [, work] = serverWorkClient(
        patchedTracer(),
        remoteParent({ traceState: verboseTraceState() })
      );
      assert.deepStrictEqual(names(exporter), [
        'GET /',
        'GET upstream',
        'work',
      ]);
      assert.strictEqual(
        parentId(exported(exporter)['GET upstream']),
        work.spanContext().spanId
      );
    });

    // The patch is on the shared prototype, so it applies to every provider; with a spec sampler it never fires.
    it('should change nothing with a spec sampler and no MuzzleSampler', () => {
      const [, work] = serverWorkClient(patchedTracer(parentBased()));
      assert.deepStrictEqual(names(exporter), [
        'GET /',
        'GET upstream',
        'work',
      ]);
      assert.strictEqual(
        parentId(exported(exporter)['GET upstream']),
        work.spanContext().spanId
      );

      const spans = serverWorkClient(
        patchedTracer(new AlwaysOffSampler()),
        remoteParent()
      );
      assert.strictEqual(
        new Set(spans.map(span => span.spanContext().spanId)).size,
        3
      );
      for (const span of spans)
        assert.strictEqual(span.spanContext().traceFlags, TraceFlags.NONE);
    });

    it('should lose the subtree of a dropped span without the patch', () => {
      // The SDK's span for "work" is unsampled, so the ParentBased delegate drops the CLIENT span below it.
      serverWorkClient(
        sdkProvider(exporter, parentBasedMuzzle()).getTracer('test')
      );
      assert.deepStrictEqual(names(exporter), ['GET /']);
    });
  });
});

describe('patchTracerPrototype', () => {
  it('should wrap once and keep the original', () => {
    const original = function startSpan() {
      return trace.wrapSpanContext(INVALID_SPAN_CONTEXT);
    };
    const proto = { startSpan: original };
    assert.strictEqual(isPatched(proto.startSpan), false);
    assert.strictEqual(patchTracerPrototype(proto), true);
    const patched = proto.startSpan;
    assert.notStrictEqual(patched, original);
    assert.ok(isPatched(patched));
    assert.strictEqual(
      (patched as unknown as Record<symbol, unknown>)[ORIGINAL_START_SPAN],
      original
    );
    assert.strictEqual(patchTracerPrototype(proto), false);
    assert.strictEqual(proto.startSpan, patched);
  });

  it('should leave alone a prototype that another copy of otel-muzzle patched', () => {
    // Another copy has its own wrapStartSpan; ORIGINAL_START_SPAN is Symbol.for, so both see the mark.
    const foreign = Object.assign(() => undefined, {
      [Symbol.for('otel-muzzle.original-startSpan')]: () => undefined,
    });
    const proto = { startSpan: foreign };
    assert.strictEqual(patchTracerPrototype(proto as never), false);
    assert.strictEqual(proto.startSpan, foreign);
  });
});
