// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import { readFileSync } from 'node:fs';
import {
  createTraceState,
  ROOT_CONTEXT,
  SpanKind,
  trace,
  type Attributes,
  type Context,
  type Link,
} from '@opentelemetry/api';
import {
  AlwaysOffSampler,
  AlwaysOnSampler,
  ParentBasedSampler,
  SamplingDecision,
  type Sampler,
  type SamplingResult,
} from '@opentelemetry/sdk-trace';
import { MuzzleSampler } from '../src/muzzle-sampler';
import {
  localParent,
  LOUD_TRACE_ID,
  parentBasedMuzzle,
  QUIET_TRACE_ID,
  RATIO_ENV_VECTORS,
  RATIO_VECTORS,
  remoteParent,
  verboseTraceState,
  withDiag,
  withEnv,
} from './helpers';

/** A delegate that returns a fixed result and records what it was asked. */
class FixedSampler implements Sampler {
  readonly calls: unknown[][] = [];
  constructor(private readonly result: SamplingResult) {}
  shouldSample(...args: Parameters<Sampler['shouldSample']>): SamplingResult {
    this.calls.push(args);
    return this.result;
  }
  toString(): string {
    return 'Fixed';
  }
}

const RECORD_AND_SAMPLED: SamplingResult = {
  decision: SamplingDecision.RECORD_AND_SAMPLED,
};

/**
 * Builds a MuzzleSampler with no diag logger set, then makes one sampling decision with `diag` captured. Returns the
 * sampler and the messages logged by that decision.
 */
function constructThenSample(
  options?: ConstructorParameters<typeof MuzzleSampler>[0]
): [MuzzleSampler, string[]] {
  const muzzle = new MuzzleSampler(options);
  const [, messages] = withDiag(() =>
    muzzle.shouldSample(
      ROOT_CONTEXT,
      QUIET_TRACE_ID,
      'span',
      SpanKind.SERVER,
      {},
      []
    )
  );
  return [muzzle, messages];
}

/** A MuzzleSampler at `ratio` over a delegate that samples everything. */
function atRatio(
  ratio: number,
  delegate: Sampler = new AlwaysOnSampler()
): MuzzleSampler {
  return new MuzzleSampler({ ratio, delegate });
}

/** The sampler's result for one span; the trace id is the parent's, or QUIET_TRACE_ID for a root. */
function sample(
  muzzle: Sampler,
  ctx: Context,
  kind: SpanKind | undefined = SpanKind.INTERNAL,
  attributes: Attributes = {},
  links: Link[] = []
): SamplingResult {
  const traceId = trace.getSpanContext(ctx)?.traceId ?? QUIET_TRACE_ID;
  return muzzle.shouldSample(
    ctx,
    traceId,
    'span',
    kind as SpanKind,
    attributes,
    links
  );
}

const isSampled = (result: SamplingResult) =>
  result.decision === SamplingDecision.RECORD_AND_SAMPLED;
const isDropped = (result: SamplingResult) =>
  result.decision === SamplingDecision.NOT_RECORD;

describe('MuzzleSampler keep rule', () => {
  it('should drop an INTERNAL span under a local parent', () => {
    assert.ok(isDropped(sample(parentBasedMuzzle(), localParent())));
  });

  it('should count a missing kind as INTERNAL', () => {
    assert.ok(isDropped(sample(parentBasedMuzzle(), localParent(), undefined)));
  });

  it('should keep every other kind under a local parent', () => {
    for (const kind of [
      SpanKind.SERVER,
      SpanKind.CLIENT,
      SpanKind.PRODUCER,
      SpanKind.CONSUMER,
    ]) {
      assert.ok(
        isSampled(sample(parentBasedMuzzle(), localParent(), kind)),
        SpanKind[kind]
      );
    }
  });

  it('should keep an INTERNAL root', () => {
    assert.ok(isSampled(sample(parentBasedMuzzle(), ROOT_CONTEXT)));
  });

  it('should keep an INTERNAL span under an invalid parent', () => {
    const invalid = trace.setSpanContext(ROOT_CONTEXT, {
      traceId: '0'.repeat(32),
      spanId: '0'.repeat(16),
      traceFlags: 1,
    });
    assert.ok(
      isSampled(
        parentBasedMuzzle().shouldSample(
          invalid,
          QUIET_TRACE_ID,
          'span',
          SpanKind.INTERNAL,
          {},
          []
        )
      )
    );
  });

  it('should keep an INTERNAL span under a remote parent', () => {
    assert.ok(isSampled(sample(parentBasedMuzzle(), remoteParent())));
  });

  it('should keep an INTERNAL span when the tracestate says muzzle=verbose', () => {
    assert.ok(
      isSampled(
        sample(
          parentBasedMuzzle(),
          localParent({ traceState: verboseTraceState() })
        )
      )
    );
  });

  it('should not force verbose for other tracestate values', () => {
    for (const header of [
      'muzzle=VERBOSE',
      'muzzle=quiet',
      'vendor=verbose',
      'muzzle=verbose1',
    ]) {
      const result = sample(
        parentBasedMuzzle(),
        localParent({ traceState: createTraceState(header) })
      );
      assert.ok(isDropped(result), header);
    }
  });

  it('should keep an INTERNAL span when the ratio selects the trace id', () => {
    assert.ok(
      isSampled(sample(atRatio(0.1), localParent({ traceId: LOUD_TRACE_ID })))
    );
    assert.ok(
      isDropped(sample(atRatio(0.1), localParent({ traceId: QUIET_TRACE_ID })))
    );
  });

  it('should never treat a malformed trace id as verbose, even at ratio 1', () => {
    // The SDK only passes valid ids; parseInt alone would read '-1234567' and '12zzzzzz' as numbers.
    const ctx = localParent({ traceId: QUIET_TRACE_ID });
    const head = QUIET_TRACE_ID.slice(0, 24);
    for (const traceId of [
      `${head}-1234567`,
      `${head}12zzzzzz`,
      '00000000000000000000000000000000',
      'abc',
      '',
    ]) {
      assert.ok(
        isDropped(
          atRatio(1).shouldSample(
            ctx,
            traceId,
            'span',
            SpanKind.INTERNAL,
            {},
            []
          )
        ),
        traceId
      );
    }
  });

  it('should match every case of testdata/ratio_vectors.json', () => {
    const { cases } = JSON.parse(readFileSync(RATIO_VECTORS, 'utf8')) as {
      cases: Array<{ trace_id: string; ratio: number; verbose: boolean }>;
    };
    assert.ok(cases.length >= 100, `only ${cases.length} cases`);
    for (const c of cases) {
      const result = sample(
        atRatio(c.ratio),
        localParent({ traceId: c.trace_id })
      );
      assert.strictEqual(isSampled(result), c.verbose, JSON.stringify(c));
    }
  });
});

describe('MuzzleSampler and its delegate', () => {
  it("should let the delegate's DROP win, even for a SERVER span under a verbose remote parent", () => {
    const delegate = new FixedSampler({
      decision: SamplingDecision.NOT_RECORD,
      traceState: verboseTraceState(),
    });
    const ctx = remoteParent({
      traceId: LOUD_TRACE_ID,
      traceState: verboseTraceState(),
    });
    assert.ok(isDropped(sample(atRatio(1, delegate), ctx, SpanKind.SERVER)));
    assert.ok(
      isDropped(sample(atRatio(1, delegate), ROOT_CONTEXT, SpanKind.SERVER))
    );
  });

  it("should return the delegate's result unchanged for a kept span", () => {
    const result: SamplingResult = {
      decision: SamplingDecision.RECORD,
      attributes: { a: 1 },
      traceState: createTraceState('v=1'),
    };
    assert.strictEqual(
      sample(
        parentBasedMuzzle(new FixedSampler(result)),
        localParent(),
        SpanKind.CLIENT
      ),
      result
    );
  });

  it('should count a record-only result as recording, so the keep rule still drops', () => {
    const delegate = new FixedSampler({ decision: SamplingDecision.RECORD });
    assert.ok(isDropped(sample(parentBasedMuzzle(delegate), localParent())));
  });

  it('should pass the original arguments to the delegate', () => {
    const delegate = new FixedSampler(RECORD_AND_SAMPLED);
    const ctx = localParent();
    const attributes = { k: 'v' };
    const links: Link[] = [{ context: trace.getSpanContext(remoteParent())! }];
    parentBasedMuzzle(delegate).shouldSample(
      ctx,
      QUIET_TRACE_ID,
      'name',
      SpanKind.CLIENT,
      attributes,
      links
    );
    assert.strictEqual(delegate.calls.length, 1);
    const [gotCtx, traceId, name, kind, gotAttributes, gotLinks] =
      delegate.calls[0];
    assert.strictEqual(gotCtx, ctx);
    assert.deepStrictEqual(
      [traceId, name, kind],
      [QUIET_TRACE_ID, 'name', SpanKind.CLIENT]
    );
    assert.strictEqual(gotAttributes, attributes);
    assert.strictEqual(gotLinks, links);
  });

  it("should keep the delegate's tracestate on a drop", () => {
    const traceState = createTraceState('vendor=x');
    const delegate = new FixedSampler({
      decision: SamplingDecision.RECORD_AND_SAMPLED,
      traceState,
    });
    const result = sample(
      parentBasedMuzzle(delegate),
      localParent({ traceState: createTraceState('parent=y') })
    );
    assert.ok(isDropped(result));
    assert.strictEqual(result.traceState, traceState);
  });

  it("should fall back to the parent's tracestate on a drop", () => {
    const traceState = createTraceState('vendor=x');
    const result = sample(
      parentBasedMuzzle(new FixedSampler(RECORD_AND_SAMPLED)),
      localParent({ traceState })
    );
    assert.ok(isDropped(result));
    assert.strictEqual(result.traceState, traceState);
  });

  it('should drop with no attributes', () => {
    const delegate = new FixedSampler({
      decision: SamplingDecision.RECORD_AND_SAMPLED,
      attributes: { a: 1 },
    });
    assert.deepStrictEqual(sample(parentBasedMuzzle(delegate), localParent()), {
      decision: SamplingDecision.NOT_RECORD,
      traceState: undefined,
    });
  });
});

describe('MuzzleSampler defaults', () => {
  const NO_ENV = {
    OTEL_TRACES_SAMPLER: undefined,
    OTEL_TRACES_SAMPLER_ARG: undefined,
    OTEL_MUZZLE_VERBOSE_RATIO: undefined,
  };

  it('should default the delegate to ParentBased(root: AlwaysOn)', () => {
    withEnv(NO_ENV, () => {
      const muzzle = new MuzzleSampler({ ratio: 0 });
      assert.ok(muzzle.delegate instanceof ParentBasedSampler);
      assert.match(
        muzzle.toString(),
        /delegate=ParentBased\{root=AlwaysOnSampler,/
      );
      assert.ok(
        isDropped(
          sample(muzzle, remoteParent({ sampled: false }), SpanKind.SERVER)
        )
      );
      assert.ok(isSampled(sample(muzzle, ROOT_CONTEXT, SpanKind.SERVER)));
    });
  });

  it('should default the delegate to the one OTEL_TRACES_SAMPLER selects, as NodeSDK does', () => {
    withEnv({ ...NO_ENV, OTEL_TRACES_SAMPLER: 'always_off' }, () => {
      const muzzle = new MuzzleSampler({ ratio: 0 });
      assert.ok(muzzle.delegate instanceof AlwaysOffSampler);
      assert.ok(isDropped(sample(muzzle, ROOT_CONTEXT, SpanKind.SERVER)));
    });
  });

  it('should prefer an explicit delegate over OTEL_TRACES_SAMPLER', () => {
    withEnv({ ...NO_ENV, OTEL_TRACES_SAMPLER: 'always_off' }, () => {
      const delegate = new AlwaysOnSampler();
      assert.strictEqual(new MuzzleSampler({ delegate }).delegate, delegate);
    });
  });

  it('should default the ratio to 1 in 10', () => {
    withEnv(NO_ENV, () => {
      assert.strictEqual(new MuzzleSampler().ratio, 0.1);
      assert.strictEqual(new MuzzleSampler({ ratio: undefined }).ratio, 0.1);
    });
  });

  it('should read the ratio from OTEL_MUZZLE_VERBOSE_RATIO, ignoring whitespace', () => {
    withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: ' 0.25 ' }, () => {
      const [muzzle, messages] = constructThenSample();
      assert.strictEqual(muzzle.ratio, 0.25);
      assert.deepStrictEqual(messages, []);
    });
    for (const [raw, ratio] of [
      ['0', 0],
      ['1', 1],
      ['1.0', 1],
      ['.5', 0.5],
      ['1e-1', 0.1],
      ['', 0.1],
      ['  ', 0.1],
    ] as const) {
      withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: raw }, () => {
        assert.strictEqual(
          new MuzzleSampler().ratio,
          ratio,
          JSON.stringify(raw)
        );
      });
    }
  });

  it('should warn and fall back to 0.1 for an invalid OTEL_MUZZLE_VERBOSE_RATIO', () => {
    for (const raw of [
      'abc',
      '1.5',
      '-0.1',
      'NaN',
      'Infinity',
      '0x0',
      '1/2',
      '0.5abc',
    ]) {
      withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: raw }, () => {
        const [muzzle, messages] = constructThenSample();
        assert.strictEqual(muzzle.ratio, 0.1, raw);
        assert.deepStrictEqual(messages, [
          `WARN otel-muzzle OTEL_MUZZLE_VERBOSE_RATIO must be a number within [0, 1], got ${JSON.stringify(raw)}; using 0.1`,
        ]);
      });
    }
  });

  it('should log configuration warnings on the first sampling decision, once', () => {
    withEnv(
      {
        ...NO_ENV,
        OTEL_MUZZLE_VERBOSE_RATIO: 'abc',
        OTEL_TRACES_SAMPLER: 'bogus',
      },
      () => {
        // No diag logger is set here, as in `new NodeSDK({ sampler: new MuzzleSampler() })`.
        const [constructed, atConstruction] = withDiag(
          () => new MuzzleSampler()
        );
        assert.deepStrictEqual(atConstruction, []);
        const [, messages] = withDiag(() => {
          for (let i = 0; i < 2; i++) {
            constructed.shouldSample(
              ROOT_CONTEXT,
              QUIET_TRACE_ID,
              'span',
              SpanKind.SERVER,
              {},
              []
            );
          }
        });
        assert.deepStrictEqual(messages, [
          'WARN otel-muzzle unknown OTEL_TRACES_SAMPLER value "bogus", using the default sampler',
          'WARN otel-muzzle OTEL_MUZZLE_VERBOSE_RATIO must be a number within [0, 1], got "abc"; using 0.1',
        ]);
      }
    );
  });

  it('should parse OTEL_MUZZLE_VERBOSE_RATIO as every case of testdata/ratio_env_vectors.json says', () => {
    const { cases } = JSON.parse(readFileSync(RATIO_ENV_VECTORS, 'utf8')) as {
      cases: { raw: string; ratio: number; warns: boolean }[];
    };
    assert.ok(cases.length > 40);
    for (const { raw, ratio, warns } of cases) {
      withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: raw }, () => {
        const [muzzle, messages] = constructThenSample();
        assert.strictEqual(muzzle.ratio, ratio, JSON.stringify(raw));
        if (warns) {
          assert.strictEqual(messages.length, 1, JSON.stringify(raw));
          assert.match(
            messages[0],
            /^WARN otel-muzzle OTEL_MUZZLE_VERBOSE_RATIO must be a number within \[0, 1\], got .*; using 0\.1$/s
          );
        } else {
          assert.deepStrictEqual(messages, [], JSON.stringify(raw));
        }
      });
    }
  });

  it('should prefer an explicit ratio over the environment, without reading it', () => {
    withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: 'abc' }, () => {
      const [muzzle, messages] = withDiag(
        () => new MuzzleSampler({ ratio: 0.5 })
      );
      assert.strictEqual(muzzle.ratio, 0.5);
      assert.deepStrictEqual(messages, []);
    });
  });

  it('should throw for an invalid ratio argument', () => {
    for (const ratio of [
      -0.1,
      1.1,
      Number.NaN,
      Infinity,
      -Infinity,
      '0.5',
      true,
      false,
      null,
      {},
    ]) {
      assert.throws(
        () => new MuzzleSampler({ ratio: ratio as number }),
        /otel-muzzle: ratio must be a number within \[0, 1\]/,
        String(ratio)
      );
    }
  });

  it('should name the ratio and the delegate in its description', () => {
    assert.strictEqual(
      new MuzzleSampler({
        ratio: 0.1,
        delegate: new FixedSampler(RECORD_AND_SAMPLED),
      }).toString(),
      'MuzzleSampler{ratio=0.1, delegate=Fixed}'
    );
    assert.strictEqual(
      new MuzzleSampler({
        ratio: 1,
        delegate: new AlwaysOnSampler(),
      }).toString(),
      'MuzzleSampler{ratio=1, delegate=AlwaysOnSampler}'
    );
  });
});
