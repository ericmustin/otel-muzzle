// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import {
  context,
  DiagLogLevel,
  ProxyTracerProvider,
  SpanKind,
  trace,
  type Tracer,
  type TracerProvider as ApiTracerProvider,
} from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { NodeSDK } from '@opentelemetry/sdk-node';
import {
  AlwaysOnSampler,
  InMemorySpanExporter,
  SimpleSpanProcessor,
  TracerProvider,
  type Sampler,
} from '@opentelemetry/sdk-trace';
import { installMuzzle } from '../src/install';
import { ORIGINAL_START_SPAN } from '../src/internal-types';
import { isPatched } from '../src/patch';
import {
  exported,
  inSpan,
  names,
  parentBasedMuzzle,
  parentId,
  remoteParent,
  sdkProvider,
  serverWorkClient,
  unpatchTracerPrototype,
  withDiag,
  withEnv,
} from './helpers';

type Proto = {
  startSpan: Tracer['startSpan'] & {
    [ORIGINAL_START_SPAN]?: Tracer['startSpan'];
  };
};
const SDK_TRACER_PROTO: Proto = Object.getPrototypeOf(
  new TracerProvider().getTracer('probe')
);
const SDK_START_SPAN = SDK_TRACER_PROTO.startSpan;

let exporter: InMemorySpanExporter;

/** Runs SERVER -> INTERNAL "work" -> CLIENT under a sampled remote parent: "work" drops, its child moves up. */
function assertReparented(tracer: Tracer): void {
  const [server] = serverWorkClient(tracer, remoteParent());
  assert.deepStrictEqual(names(exporter), ['GET /', 'GET upstream']);
  assert.strictEqual(
    parentId(exported(exporter)['GET upstream']),
    server.spanContext().spanId
  );
}

/** installMuzzle() with diag captured at DEBUG: its result and every message. */
function install(tracerProvider?: ApiTracerProvider): [boolean, string[]] {
  return withDiag(
    () => installMuzzle(tracerProvider ? { tracerProvider } : undefined),
    DiagLogLevel.DEBUG
  );
}

describe('installMuzzle', () => {
  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    context.setGlobalContextManager(
      new AsyncLocalStorageContextManager().enable()
    );
  });
  afterEach(() => {
    // installMuzzle patches the SDK's Tracer prototype; never let that leak into the next test.
    unpatchTracerPrototype(SDK_TRACER_PROTO);
    trace.disable();
    context.disable();
  });

  it('should patch the SDK Tracer prototype and reparent, silently when the sampler is a MuzzleSampler', () => {
    const provider = sdkProvider(exporter, parentBasedMuzzle());
    assert.deepStrictEqual(install(provider), [true, []]);
    assert.ok(isPatched(SDK_TRACER_PROTO.startSpan));
    assert.strictEqual(
      SDK_TRACER_PROTO.startSpan[ORIGINAL_START_SPAN],
      SDK_START_SPAN
    );
    assertReparented(provider.getTracer('app'));
  });

  it('should reach tracers created before it', () => {
    const provider = sdkProvider(exporter, parentBasedMuzzle());
    const tracer = provider.getTracer('early');
    assert.deepStrictEqual(install(provider), [true, []]);
    assertReparented(tracer);
  });

  it('should reach API proxy tracers obtained before the provider was registered', () => {
    const proxyTracer = trace.getTracer('app');
    assert.ok(
      trace.setGlobalTracerProvider(sdkProvider(exporter, parentBasedMuzzle()))
    );
    assert.deepStrictEqual(install(), [true, []]);
    assertReparented(proxyTracer);
  });

  it('should unwrap a ProxyTracerProvider passed explicitly', () => {
    const proxy = new ProxyTracerProvider();
    proxy.setDelegate(sdkProvider(exporter, parentBasedMuzzle()));
    assert.deepStrictEqual(install(proxy), [true, []]);
    assertReparented(proxy.getTracer('app'));
  });

  it('should be idempotent, also across providers that share the prototype', () => {
    const first = sdkProvider(exporter, parentBasedMuzzle());
    assert.deepStrictEqual(install(first), [true, []]);
    const patched = SDK_TRACER_PROTO.startSpan;
    assert.deepStrictEqual(install(first), [true, []]);
    assert.deepStrictEqual(
      install(sdkProvider(new InMemorySpanExporter(), parentBasedMuzzle())),
      [true, []]
    );
    assert.strictEqual(SDK_TRACER_PROTO.startSpan, patched);
    assert.strictEqual(
      SDK_TRACER_PROTO.startSpan[ORIGINAL_START_SPAN],
      SDK_START_SPAN
    );
    assertReparented(first.getTracer('app'));
  });

  it('should reparent in a provider that NodeSDK started', async () => {
    context.disable(); // NodeSDK registers its own context manager
    const sdk = new NodeSDK({
      sampler: parentBasedMuzzle(),
      spanProcessors: [new SimpleSpanProcessor({ exporter })],
      instrumentations: [],
      textMapPropagator: null,
      metricReaders: [],
      logRecordProcessors: [],
      autoDetectResources: false,
    });
    sdk.start();
    try {
      assert.deepStrictEqual(install(), [true, []]);
      assertReparented(trace.getTracer('app'));
    } finally {
      await sdk.shutdown();
    }
  });

  it('should warn when the provider does not sample with a MuzzleSampler, and still patch', () => {
    const provider = sdkProvider(exporter, new AlwaysOnSampler());
    const [installed, messages] = install(provider);
    assert.strictEqual(installed, true);
    assert.deepStrictEqual(messages, [
      'WARN otel-muzzle the tracer provider samples with AlwaysOnSampler, not a MuzzleSampler, so no spans are dropped. ' +
        'Pass sampler: new MuzzleSampler() to NodeSDK or to the TracerProvider.',
    ]);
    assert.ok(isPatched(SDK_TRACER_PROTO.startSpan));
    // ...and the patch changes nothing there.
    inSpan(
      provider.getTracer('app'),
      'GET /',
      { kind: SpanKind.SERVER },
      () => {
        inSpan(provider.getTracer('app'), 'work', {}, () => {});
      }
    );
    assert.deepStrictEqual(names(exporter), ['GET /', 'work']);
  });

  it('should recognize a MuzzleSampler from another copy of otel-muzzle', () => {
    const foreign = {
      [Symbol.for('otel-muzzle.MuzzleSampler')]: true,
      shouldSample: parentBasedMuzzle().shouldSample,
      toString: () => 'MuzzleSampler{}',
    };
    assert.deepStrictEqual(
      install(sdkProvider(exporter, foreign as unknown as Sampler)),
      [true, []]
    );
  });

  it('should patch without guessing when the tracer has no readable sampler field', () => {
    // Stands in for an SDK whose Tracer keeps its sampler somewhere else: no false warning.
    class OtherTracer {
      readonly instrumentationScope = { name: 'x' };
      startSpan(): never {
        throw new Error('not called');
      }
    }
    const original = OtherTracer.prototype.startSpan;
    const provider = {
      getTracer: () => new OtherTracer(),
    } as unknown as ApiTracerProvider;
    assert.deepStrictEqual(install(provider), [true, []]);
    assert.ok(isPatched(OtherTracer.prototype.startSpan));
    assert.strictEqual(
      (OtherTracer.prototype.startSpan as Proto['startSpan'])[
        ORIGINAL_START_SPAN
      ],
      original
    );
  });

  it('should warn, return false and patch nothing when no SDK provider is registered', () => {
    withEnv({ OTEL_SDK_DISABLED: undefined }, () => {
      const [installed, messages] = install();
      assert.strictEqual(installed, false);
      assert.strictEqual(messages.length, 1);
      assert.match(
        messages[0],
        /^WARN otel-muzzle no OpenTelemetry SDK TracerProvider is registered, so installMuzzle\(\) did nothing/
      );
      assert.match(messages[0], /after NodeSDK.start\(\)/);
    });
    assert.strictEqual(SDK_TRACER_PROTO.startSpan, SDK_START_SPAN);
  });

  it('should refuse a provider whose tracers are not SDK tracers', () => {
    for (const tracer of [
      { startSpan: () => undefined },
      trace.getTracerProvider().getTracer('noop'),
    ]) {
      const [installed, messages] = install({
        getTracer: () => tracer,
      } as unknown as ApiTracerProvider);
      assert.strictEqual(installed, false);
      assert.match(messages[0], /no OpenTelemetry SDK TracerProvider/);
    }
    assert.strictEqual(SDK_TRACER_PROTO.startSpan, SDK_START_SPAN);
  });

  it('should return false quietly with OTEL_SDK_DISABLED=true and no SDK provider', () => {
    for (const value of ['true', ' TRUE ']) {
      withEnv({ OTEL_SDK_DISABLED: value }, () => {
        const [installed, messages] = install();
        assert.strictEqual(installed, false);
        assert.deepStrictEqual(messages, [
          'DEBUG otel-muzzle OTEL_SDK_DISABLED=true, so installMuzzle() did nothing',
        ]);
      });
    }
    assert.strictEqual(SDK_TRACER_PROTO.startSpan, SDK_START_SPAN);
  });

  it('should do nothing when OTEL_SDK_DISABLED=true keeps NodeSDK from registering a provider', () => {
    withEnv({ OTEL_SDK_DISABLED: 'true' }, () => {
      const sdk = new NodeSDK({
        sampler: parentBasedMuzzle(),
        spanProcessors: [new SimpleSpanProcessor({ exporter })],
        instrumentations: [],
      });
      sdk.start();
      assert.strictEqual(install()[0], false);
    });
    assert.strictEqual(SDK_TRACER_PROTO.startSpan, SDK_START_SPAN);
  });
});
