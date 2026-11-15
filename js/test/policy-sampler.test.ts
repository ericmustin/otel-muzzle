// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import {
  copyFileSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  context,
  createTraceState,
  ROOT_CONTEXT,
  SpanKind,
  trace,
  TraceFlags,
  type Attributes,
  type Context,
  type Link,
  type TraceState,
  type Tracer,
} from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import {
  AlwaysOffSampler,
  AlwaysOnSampler,
  InMemorySpanExporter,
  SamplingDecision,
  TracerProvider,
  type Sampler,
  type SamplingResult,
} from '@opentelemetry/sdk-trace';
import { installMuzzle } from '../src/install';
import { isMuzzleSampler, MuzzleSampler } from '../src/muzzle-sampler';
import { parsePolicySet } from '../src/policy/parser';
import { fromFile, providerFor, reset } from '../src/policy/runtime';
import { SpanSamplingImplementer } from '../src/policy/span-sampling';
import {
  EXAMPLE_POLICY,
  exported,
  inSpan,
  localParent,
  names,
  parentId,
  POLICY_DROP_TRACE_ID,
  POLICY_KEEP_TRACE_ID,
  policyVectors,
  remoteParent,
  sdkProvider,
  unpatchTracerPrototype,
  verboseTraceState,
  withDiag,
  withEnv,
} from './helpers';

type Pairs = Array<[key: string, value: string]>;

interface DecisionVectors {
  policy_sets: Record<string, { text: string; malformed: boolean }>;
  cases: Array<{
    set: string;
    name: string;
    span: {
      trace_id: string;
      name: string;
      kind: keyof typeof SpanKind | null;
      parent: 'none' | 'local' | 'remote';
      parent_span_id: string | null;
      tracestate: Pairs;
      result_tracestate: Pairs | null;
      attributes: Attributes | null;
    };
    keep: boolean;
    drop_tracestate?: Record<string, string>;
  }>;
}

const KEEP_ALL =
  '{"policies": [{"id": "all", "spanSampling": {"sampling": {"percentage": 100}}}]}';
const DROP_ALL =
  '{"policies": [{"id": "none", "spanSampling": {"sampling": {"percentage": 0}}}]}';
const NO_ENV = {
  OTEL_TRACES_SAMPLER: undefined,
  OTEL_TRACES_SAMPLER_ARG: undefined,
  OTEL_MUZZLE_VERBOSE_RATIO: undefined,
  OTEL_MUZZLE_POLICY_FILE: undefined,
};
const SDK_TRACER_PROTO = Object.getPrototypeOf(
  new TracerProvider().getTracer('probe')
);

/** A delegate that records every span and returns a fixed tracestate of its own, or none. */
class Recording implements Sampler {
  readonly calls: unknown[][] = [];
  readonly result: SamplingResult;
  constructor(
    traceState?: TraceState,
    decision = SamplingDecision.RECORD_AND_SAMPLED
  ) {
    this.result = { decision, attributes: { from: 'delegate' }, traceState };
  }
  shouldSample(...args: Parameters<Sampler['shouldSample']>): SamplingResult {
    this.calls.push(args);
    return this.result;
  }
  toString(): string {
    return 'Recording';
  }
}

/** The policies of a policy file's text. Their warnings are dropped. */
function policiesOf(text: string): SpanSamplingImplementer {
  const policies = new SpanSamplingImplementer(() => {});
  policies.applyPolicies(parsePolicySet(text));
  return policies;
}

const example = () => policiesOf(readFileSync(EXAMPLE_POLICY, 'utf8'));

/** The sampler's result for one span; the trace id is the parent's. */
function sample(
  muzzle: Sampler,
  ctx: Context,
  kind: SpanKind = SpanKind.INTERNAL,
  name = 'work',
  attributes: Attributes = {},
  links: Link[] = []
): SamplingResult {
  const traceId = trace.getSpanContext(ctx)?.traceId ?? POLICY_DROP_TRACE_ID;
  return muzzle.shouldSample(ctx, traceId, name, kind, attributes, links);
}

/** A tracestate as a mapping. */
function tracestateOf(result: SamplingResult): Record<string, string> {
  const header = result.traceState?.serialize() ?? '';
  return Object.fromEntries(
    header
      .split(',')
      .filter(member => member !== '')
      .map(member => {
        const at = member.indexOf('=');
        return [member.slice(0, at), member.slice(at + 1)];
      })
  );
}

const isSampled = (result: SamplingResult) =>
  result.decision === SamplingDecision.RECORD_AND_SAMPLED;
const isDropped = (result: SamplingResult) =>
  result.decision === SamplingDecision.NOT_RECORD;

let directory: string;
/** A copy of the example policy, in a directory of its own. */
let policyFile: string;

function useTemporaryPolicyFile(): void {
  beforeEach(() => {
    reset();
    directory = mkdtempSync(path.join(os.tmpdir(), 'otel-muzzle-'));
    policyFile = path.join(directory, 'policy.json');
    copyFileSync(EXAMPLE_POLICY, policyFile);
  });
  afterEach(() => {
    reset();
    rmSync(directory, { recursive: true, force: true });
  });
}

describe('MuzzleSampler in policy mode', () => {
  describe('decisions', () => {
    /**
     * Runs every case of testdata/policy_decision_vectors.json through a MuzzleSampler.
     *
     * @param toTraceState How a tracestate is built from its members.
     */
    function assertDecisionVectors(
      toTraceState: (pairs: Pairs) => TraceState | undefined
    ): void {
      const vectors = policyVectors<DecisionVectors>('decision');
      const policies = new Map<string, SpanSamplingImplementer>();
      for (const [name, set] of Object.entries(vectors.policy_sets)) {
        // A malformed document leaves no policy in effect.
        policies.set(name, policiesOf(set.malformed ? '{}' : set.text));
      }
      assert.ok(vectors.cases.length > 190, `${vectors.cases.length} cases`);
      for (const c of vectors.cases) {
        const { span } = c;
        const delegate = new Recording(
          span.result_tracestate === null
            ? undefined
            : toTraceState(span.result_tracestate)
        );
        const muzzle = new MuzzleSampler({
          delegate,
          policies: policies.get(c.set),
        });
        const ctx =
          span.parent === 'none'
            ? ROOT_CONTEXT
            : trace.setSpanContext(ROOT_CONTEXT, {
                traceId: span.trace_id,
                spanId: span.parent_span_id!,
                traceFlags: TraceFlags.SAMPLED,
                isRemote: span.parent === 'remote',
                traceState: toTraceState(span.tracestate),
              });
        const result = muzzle.shouldSample(
          ctx,
          span.trace_id,
          span.name,
          (span.kind === null ? undefined : SpanKind[span.kind]) as SpanKind,
          (span.attributes ?? undefined) as Attributes,
          []
        );
        const label = `${c.set}: ${c.name}`;
        if (c.keep) {
          assert.strictEqual(result, delegate.result, label);
        } else {
          assert.strictEqual(
            result.decision,
            SamplingDecision.NOT_RECORD,
            label
          );
          assert.deepStrictEqual(
            tracestateOf(result),
            c.drop_tracestate,
            label
          );
          assert.strictEqual(result.attributes, undefined, label);
        }
        assert.strictEqual(delegate.calls.length, 1, label);
      }
    }

    const header = (pairs: Pairs) =>
      pairs.map(([key, value]) => `${key}=${value}`).join(',');

    it('should decide every case of testdata/policy_decision_vectors.json', () => {
      // A span context without tracestate members has no tracestate, as the SDK and the propagator leave it.
      assertDecisionVectors(pairs =>
        pairs.length === 0 ? undefined : createTraceState(header(pairs))
      );
    });

    it('should decide every case the same with an empty tracestate object', () => {
      assertDecisionVectors(pairs => createTraceState(header(pairs)));
    });

    it("should let the delegate's DROP win", () => {
      for (const delegate of [
        new AlwaysOffSampler(),
        new Recording(undefined, SamplingDecision.NOT_RECORD),
      ]) {
        const muzzle = new MuzzleSampler({
          delegate,
          policies: policiesOf(KEEP_ALL),
        });
        const result = sample(
          muzzle,
          remoteParent({ traceId: POLICY_KEEP_TRACE_ID }),
          SpanKind.SERVER
        );
        assert.ok(isDropped(result));
        if (delegate instanceof Recording) {
          assert.strictEqual(result, delegate.result);
        }
      }
    });

    it("should return the delegate's result unchanged for a kept span, and pass it the original arguments", () => {
      const delegate = new Recording(createTraceState('ot=th:0,vendor=x'));
      const muzzle = new MuzzleSampler({ delegate, policies: example() });
      const ctx = localParent({ traceId: POLICY_KEEP_TRACE_ID });
      const attributes = { a: 1 };
      const links: Link[] = [
        { context: trace.getSpanContext(remoteParent())! },
      ];
      const result = muzzle.shouldSample(
        ctx,
        POLICY_KEEP_TRACE_ID,
        'work',
        SpanKind.INTERNAL,
        attributes,
        links
      );
      assert.strictEqual(result, delegate.result);
      assert.strictEqual(delegate.calls.length, 1);
      const [gotCtx, traceId, name, kind, gotAttributes, gotLinks] =
        delegate.calls[0];
      assert.strictEqual(gotCtx, ctx);
      assert.deepStrictEqual(
        [traceId, name, kind],
        [POLICY_KEEP_TRACE_ID, 'work', SpanKind.INTERNAL]
      );
      assert.strictEqual(gotAttributes, attributes);
      assert.strictEqual(gotLinks, links);
    });

    it('should drop a record-only span that the policies drop', () => {
      const muzzle = new MuzzleSampler({
        delegate: new Recording(undefined, SamplingDecision.RECORD),
        policies: example(),
      });
      assert.deepStrictEqual(
        sample(muzzle, localParent({ traceId: POLICY_DROP_TRACE_ID })),
        {
          decision: SamplingDecision.NOT_RECORD,
          traceState: undefined,
        }
      );
    });

    it('should take th out of the tracestate of a span it drops', () => {
      const traceState = createTraceState('ot=th:0;rv:00000000000000,vendor=x');
      const muzzle = new MuzzleSampler({
        delegate: new AlwaysOnSampler(),
        policies: example(),
      });
      const result = sample(
        muzzle,
        localParent({ traceId: POLICY_KEEP_TRACE_ID, traceState })
      );
      assert.ok(isDropped(result));
      assert.deepStrictEqual(tracestateOf(result), {
        ot: 'rv:00000000000000',
        vendor: 'x',
      });
      // The parent's tracestate is left as it was.
      assert.strictEqual(traceState.get('ot'), 'th:0;rv:00000000000000');
    });

    it('should remove an ot member that held only th', () => {
      const muzzle = new MuzzleSampler({
        delegate: new AlwaysOnSampler(),
        policies: policiesOf(DROP_ALL),
      });
      const result = sample(
        muzzle,
        localParent({
          traceId: POLICY_KEEP_TRACE_ID,
          traceState: createTraceState('ot=th:8,vendor=x'),
        })
      );
      assert.ok(isDropped(result));
      assert.strictEqual(result.traceState!.serialize(), 'vendor=x');
    });

    it('should return the tracestate itself for a dropped span without th', () => {
      const muzzle = new MuzzleSampler({
        delegate: new AlwaysOnSampler(),
        policies: example(),
      });
      for (const header of ['vendor=x', 'ot=rv:00000000000000', '']) {
        const traceState = createTraceState(header);
        const result = sample(muzzle, localParent({ traceState }));
        assert.ok(isDropped(result), header);
        assert.strictEqual(result.traceState, traceState, header);
      }
      const own = createTraceState('own=1');
      const fromDelegate = sample(
        new MuzzleSampler({
          delegate: new Recording(own),
          policies: example(),
        }),
        localParent({ traceState: createTraceState('parent=1') })
      );
      assert.ok(isDropped(fromDelegate));
      assert.strictEqual(fromDelegate.traceState, own);
    });

    it('should not apply the built-in rule', () => {
      const keeping = new MuzzleSampler({
        delegate: new AlwaysOnSampler(),
        policies: policiesOf(KEEP_ALL),
      });
      assert.ok(
        isSampled(
          sample(keeping, localParent({ traceId: POLICY_DROP_TRACE_ID }))
        )
      );
      const dropping = new MuzzleSampler({
        delegate: new AlwaysOnSampler(),
        policies: policiesOf(DROP_ALL),
      });
      assert.ok(
        isDropped(
          sample(
            dropping,
            remoteParent({
              traceId: POLICY_KEEP_TRACE_ID,
              traceState: verboseTraceState(),
            }),
            SpanKind.SERVER
          )
        )
      );
      assert.ok(isDropped(sample(dropping, ROOT_CONTEXT, SpanKind.SERVER)));
    });

    it('should keep a span whose trace id gives no randomness, without throwing', () => {
      const muzzle = new MuzzleSampler({
        delegate: new AlwaysOnSampler(),
        policies: policiesOf(DROP_ALL),
      });
      for (const traceId of ['', 'abc', 'z'.repeat(32)]) {
        const result = muzzle.shouldSample(
          localParent(),
          traceId,
          'work',
          SpanKind.INTERNAL,
          {},
          []
        );
        assert.ok(isSampled(result), traceId);
      }
    });
  });

  describe('configuration', () => {
    useTemporaryPolicyFile();

    it('should describe itself by its mode, and expose its policies', () => {
      const policies = example();
      const delegate = new AlwaysOnSampler();
      const muzzle = new MuzzleSampler({ delegate, policies });
      assert.strictEqual(
        muzzle.toString(),
        'MuzzleSampler{mode=policy, delegate=AlwaysOnSampler}'
      );
      assert.strictEqual(muzzle.policies, policies);
      assert.strictEqual(muzzle.ratio, undefined);
      assert.strictEqual(muzzle.delegate, delegate);
      assert.ok(isMuzzleSampler(muzzle));
    });

    it('should change nothing without a policy file', () => {
      withEnv(NO_ENV, () => {
        for (const options of [
          undefined,
          {},
          { policies: undefined, policyFile: undefined },
          { policies: null, policyFile: null },
          { policyFile: '' },
        ]) {
          const muzzle = new MuzzleSampler(
            options as ConstructorParameters<typeof MuzzleSampler>[0]
          );
          assert.strictEqual(muzzle.policies, undefined);
          assert.strictEqual(muzzle.ratio, 0.1);
          assert.match(
            muzzle.toString(),
            /^MuzzleSampler\{ratio=0\.1, delegate=ParentBased\{/
          );
        }
      });
    });

    it('should read the policy file given in code, once for every sampler', () => {
      withEnv(NO_ENV, () => {
        const muzzle = new MuzzleSampler({ policyFile });
        assert.strictEqual(muzzle.policies, fromFile(policyFile).policies);
        assert.strictEqual(muzzle.ratio, undefined);
        assert.strictEqual(
          new MuzzleSampler({ policyFile }).policies,
          muzzle.policies
        );
        assert.ok(
          isDropped(
            sample(muzzle, localParent({ traceId: POLICY_DROP_TRACE_ID }))
          )
        );
        assert.ok(
          isSampled(
            sample(muzzle, localParent({ traceId: POLICY_KEEP_TRACE_ID }))
          )
        );
      });
    });

    it('should read the policy file from OTEL_MUZZLE_POLICY_FILE', () => {
      withEnv({ ...NO_ENV, OTEL_MUZZLE_POLICY_FILE: ` ${policyFile} ` }, () => {
        const muzzle = new MuzzleSampler();
        assert.strictEqual(muzzle.policies, fromFile(policyFile).policies);
        assert.strictEqual(muzzle.ratio, undefined);
        assert.match(muzzle.toString(), /^MuzzleSampler\{mode=policy, /);
      });
    });

    it('should read a blank OTEL_MUZZLE_POLICY_FILE as unset', () => {
      for (const blank of ['', '  ']) {
        withEnv({ ...NO_ENV, OTEL_MUZZLE_POLICY_FILE: blank }, () => {
          const muzzle = new MuzzleSampler();
          assert.strictEqual(muzzle.policies, undefined);
          assert.strictEqual(muzzle.ratio, 0.1);
        });
      }
    });

    it('should prefer a policy file given in code, and policies given in code, over the environment', () => {
      const other = path.join(directory, 'other.json');
      writeFileSync(other, KEEP_ALL);
      withEnv({ ...NO_ENV, OTEL_MUZZLE_POLICY_FILE: other }, () => {
        assert.strictEqual(
          new MuzzleSampler({ policyFile }).policies,
          fromFile(policyFile).policies
        );
        const policies = example();
        assert.strictEqual(
          new MuzzleSampler({ policies, policyFile: other }).policies,
          policies
        );
      });
    });

    it('should throw for a policy file that is not a string', () => {
      for (const policyFile of [5, true, {}, ['a.json']]) {
        assert.throws(
          () =>
            new MuzzleSampler({ policyFile: policyFile as unknown as string }),
          {
            name: 'TypeError',
            message: /^otel-muzzle: policyFile must be a string, got /,
          }
        );
      }
    });

    it('should check an explicit ratio, then ignore it with a warning', () => {
      withEnv(NO_ENV, () => {
        assert.throws(
          () => new MuzzleSampler({ ratio: 2, policies: example() }),
          /otel-muzzle: ratio must be a number within \[0, 1\], got 2/
        );
        assert.throws(
          () => new MuzzleSampler({ ratio: 2, policyFile }),
          /otel-muzzle: ratio must be a number within \[0, 1\], got 2/
        );
        const [muzzle, atConstruction] = withDiag(
          () => new MuzzleSampler({ ratio: 0.5, policies: example() })
        );
        assert.strictEqual(muzzle.ratio, undefined);
        assert.deepStrictEqual(atConstruction, []);
        const [, messages] = withDiag(() => {
          sample(muzzle, ROOT_CONTEXT, SpanKind.SERVER);
          sample(muzzle, ROOT_CONTEXT, SpanKind.SERVER);
        });
        assert.deepStrictEqual(messages, [
          'WARN otel-muzzle ratio 0.5 is ignored in policy mode',
        ]);
      });
    });

    it('should warn once that OTEL_MUZZLE_VERBOSE_RATIO is ignored, and not read it', () => {
      for (const raw of ['0.5', 'abc']) {
        reset();
        withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: raw }, () => {
          const [samplers, atConstruction] = withDiag(() => [
            new MuzzleSampler({ policyFile }),
            new MuzzleSampler({ policyFile }),
          ]);
          assert.deepStrictEqual(atConstruction, []);
          const [, messages] = withDiag(() => {
            for (const muzzle of [...samplers, ...samplers]) {
              sample(muzzle, ROOT_CONTEXT, SpanKind.SERVER);
            }
          });
          assert.deepStrictEqual(messages, [
            `WARN otel-muzzle OTEL_MUZZLE_VERBOSE_RATIO is ignored, because a policy file is configured: ${policyFile}`,
          ]);
        });
      }
    });

    it('should not warn about OTEL_MUZZLE_VERBOSE_RATIO for policies given in code', () => {
      withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: 'abc' }, () => {
        const muzzle = new MuzzleSampler({ policies: example() });
        const [, messages] = withDiag(() =>
          sample(muzzle, ROOT_CONTEXT, SpanKind.SERVER)
        );
        assert.deepStrictEqual(messages, []);
      });
    });

    it('should drop nothing when the policy file cannot be read, and not fall back to the built-in rule', () => {
      withEnv(NO_ENV, () => {
        const missing = path.join(directory, 'missing.json');
        const [muzzle, atConstruction] = withDiag(
          () => new MuzzleSampler({ policyFile: missing })
        );
        assert.deepStrictEqual(atConstruction, []);
        assert.ok(muzzle.policies !== undefined && !muzzle.policies.active);
        assert.strictEqual(muzzle.ratio, undefined);
        const [results, messages] = withDiag(() => [
          sample(muzzle, localParent({ traceId: POLICY_DROP_TRACE_ID })),
          sample(muzzle, localParent({ traceId: POLICY_DROP_TRACE_ID })),
        ]);
        assert.ok(results.every(isSampled));
        assert.strictEqual(messages.length, 1);
        assert.ok(
          messages[0].startsWith(
            `WARN otel-muzzle cannot load the policy file ${missing}: ENOENT`
          ) && messages[0].endsWith('; no policy is in effect'),
          messages[0]
        );
      });
    });

    it('should log the policies that are not applied on the first sampling decision', () => {
      withEnv(NO_ENV, () => {
        writeFileSync(
          policyFile,
          JSON.stringify({
            policies: [
              { id: 'logs', logFilter: {} },
              { id: 'bad', spanSampling: { sampling: { percentage: 150 } } },
              { id: 'none', spanSampling: { sampling: { percentage: 0 } } },
            ],
          })
        );
        const muzzle = new MuzzleSampler({ policyFile });
        const [result, messages] = withDiag(() =>
          sample(muzzle, localParent({ traceId: POLICY_KEEP_TRACE_ID }))
        );
        assert.ok(isDropped(result));
        assert.deepStrictEqual(messages, [
          "WARN otel-muzzle policy 'logs' is not applied: unsupported policy type: log_filter",
          "WARN otel-muzzle policy 'bad' is not applied: policies[1].spanSampling.sampling.percentage: must be within 0 to 100",
        ]);
      });
    });
  });
});

describe('policy mode through a tracer', () => {
  useTemporaryPolicyFile();
  let exporter: InMemorySpanExporter;

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

  /** A tracer of a provider that samples by the policy file, with otel-muzzle installed. */
  function installedTracer(file: string = policyFile): Tracer {
    const provider = sdkProvider(
      exporter,
      new MuzzleSampler({ policyFile: file })
    );
    const [installed, messages] = withDiag(() =>
      installMuzzle({ tracerProvider: provider })
    );
    assert.deepStrictEqual([installed, messages], [true, []]);
    return provider.getTracer('app');
  }

  /** SERVER -> INTERNAL "work" -> INTERNAL "inner" -> CLIENT, under `parent`. */
  function serverWorkInnerClient(tracer: Tracer, parent: Context): void {
    inSpan(
      tracer,
      'GET /',
      { kind: SpanKind.SERVER },
      () =>
        inSpan(tracer, 'work', {}, () =>
          inSpan(tracer, 'inner', {}, () =>
            inSpan(tracer, 'GET upstream', { kind: SpanKind.CLIENT }, () => {})
          )
        ),
      parent
    );
  }

  const EVERY_SPAN = ['GET /', 'GET upstream', 'inner', 'work'];

  it('should export SERVER and CLIENT, with the CLIENT span under the SERVER span', () => {
    withEnv(NO_ENV, () => {
      serverWorkInnerClient(
        installedTracer(EXAMPLE_POLICY),
        remoteParent({ traceId: POLICY_DROP_TRACE_ID })
      );
      const spans = exported(exporter);
      assert.deepStrictEqual(names(exporter), ['GET /', 'GET upstream']);
      assert.strictEqual(
        parentId(spans['GET upstream']),
        spans['GET /'].spanContext().spanId
      );
      assert.strictEqual(spans['GET /'].kind, SpanKind.SERVER);
      assert.strictEqual(spans['GET upstream'].kind, SpanKind.CLIENT);
    });
  });

  it('should export every span of a trace the percentage selects', () => {
    withEnv(NO_ENV, () => {
      assert.ok(POLICY_KEEP_TRACE_ID.endsWith('ffffffffffffff'));
      serverWorkInnerClient(
        installedTracer(EXAMPLE_POLICY),
        remoteParent({ traceId: POLICY_KEEP_TRACE_ID })
      );
      const spans = exported(exporter);
      assert.deepStrictEqual(names(exporter), EVERY_SPAN);
      assert.strictEqual(
        parentId(spans['GET upstream']),
        spans['inner'].spanContext().spanId
      );
      assert.strictEqual(
        parentId(spans['inner']),
        spans['work'].spanContext().spanId
      );
      assert.strictEqual(
        parentId(spans['work']),
        spans['GET /'].spanContext().spanId
      );
    });
  });

  it('should export every span when the tracestate carries muzzle=verbose', () => {
    withEnv(NO_ENV, () => {
      serverWorkInnerClient(
        installedTracer(EXAMPLE_POLICY),
        remoteParent({
          traceId: POLICY_DROP_TRACE_ID,
          traceState: verboseTraceState(),
        })
      );
      assert.deepStrictEqual(names(exporter), EVERY_SPAN);
    });
  });

  it('should decide by an explicit rv instead of the trace id, and pass it on', () => {
    withEnv(NO_ENV, () => {
      serverWorkInnerClient(
        installedTracer(),
        remoteParent({
          traceId: POLICY_KEEP_TRACE_ID,
          traceState: createTraceState('ot=rv:00000000000000'),
        })
      );
      const spans = exported(exporter);
      assert.deepStrictEqual(names(exporter), ['GET /', 'GET upstream']);
      assert.strictEqual(
        spans['GET upstream'].spanContext().traceState?.get('ot'),
        'rv:00000000000000'
      );
    });
  });

  it('should read the policy file from the environment', () => {
    withEnv({ ...NO_ENV, OTEL_MUZZLE_POLICY_FILE: policyFile }, () => {
      const provider = sdkProvider(exporter, new MuzzleSampler());
      installMuzzle({ tracerProvider: provider });
      serverWorkInnerClient(
        provider.getTracer('app'),
        remoteParent({ traceId: POLICY_DROP_TRACE_ID })
      );
      assert.deepStrictEqual(names(exporter), ['GET /', 'GET upstream']);
    });
  });

  it('should swap the set on reload for tracers that already exist', () => {
    withEnv(NO_ENV, () => {
      const muzzle = new MuzzleSampler({ policyFile });
      const provider = sdkProvider(exporter, muzzle);
      installMuzzle({ tracerProvider: provider });
      const tracer = provider.getTracer('app');
      const parent = remoteParent({ traceId: POLICY_DROP_TRACE_ID });
      serverWorkInnerClient(tracer, parent);
      assert.deepStrictEqual(names(exporter), ['GET /', 'GET upstream']);
      exporter.reset();
      writeFileSync(policyFile, KEEP_ALL);
      assert.strictEqual(providerFor(muzzle.policies!).reload(), true);
      serverWorkInnerClient(tracer, parent);
      assert.deepStrictEqual(names(exporter), EVERY_SPAN);
    });
  });

  it('should export every span when the policy file cannot be read', () => {
    withEnv(NO_ENV, () => {
      serverWorkInnerClient(
        installedTracer(path.join(directory, 'missing.json')),
        remoteParent({ traceId: POLICY_DROP_TRACE_ID })
      );
      assert.deepStrictEqual(names(exporter), EVERY_SPAN);
    });
  });
});
