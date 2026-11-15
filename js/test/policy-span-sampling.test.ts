// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import { readFileSync } from 'node:fs';
import {
  createTraceState,
  INVALID_SPAN_CONTEXT,
  SpanKind,
  TraceFlags,
  type Attributes,
  type SpanContext,
  type TraceState,
} from '@opentelemetry/api';
import type { Policy } from '../src/policy/model';
import { parsePolicySet } from '../src/policy/parser';
import { SpanSamplingImplementer } from '../src/policy/span-sampling';
import {
  EXAMPLE_POLICY,
  LOCAL_SPAN_ID,
  POLICY_DROP_TRACE_ID,
  POLICY_KEEP_TRACE_ID,
  policyVectors,
  REMOTE_SPAN_ID,
  withDiag,
} from './helpers';

type Json = Record<string, unknown>;

interface DecisionVectors {
  policy_sets: Record<
    string,
    {
      text: string;
      malformed: boolean;
      statuses: Array<{ id: string; applied: boolean }>;
    }
  >;
}

interface SpanAtStart {
  traceId: string;
  name: string;
  kind: SpanKind | undefined;
  parent: SpanContext | undefined;
  traceState: TraceState | undefined;
  attributes: Attributes | undefined;
}

const LOCAL: SpanContext = {
  traceId: POLICY_DROP_TRACE_ID,
  spanId: LOCAL_SPAN_ID,
  traceFlags: TraceFlags.SAMPLED,
};
const KIND = { recordField: 'SPAN_RECORD_FIELD_KIND' };
const NAME = { recordField: 'SPAN_RECORD_FIELD_NAME' };
const EXISTS = { exists: {} };
const KIND_IS_INTERNAL = {
  target: KIND,
  predicate: { equals: { stringValue: 'INTERNAL' } },
};
const attribute = (...path: string[]) => ({ spanAttribute: { path } });

let warnings: string[];

/** An implementer with the given policies applied, whose warnings go to `warnings`. */
function implementer(...policies: Json[]): SpanSamplingImplementer {
  const out = new SpanSamplingImplementer(message => warnings.push(message));
  out.applyPolicies(parsePolicySet(JSON.stringify({ policies })));
  return out;
}

/** A span sampling policy as its JSON. */
function sampling(
  id: string,
  percentage: number,
  matches: Json[] = [],
  config: Json = {}
): Json {
  return {
    id,
    spanSampling: { matches, sampling: { percentage, ...config } },
  };
}

/** A policy at 0% with one matcher: a span is dropped exactly when the matcher matches. */
function dropWhen(
  target: Json,
  predicate: Json,
  negate = false
): SpanSamplingImplementer {
  return implementer(sampling('p', 0, [{ target, predicate, negate }]));
}

/** Whether the policies keep a span: by default INTERNAL "work" under a local parent, in a trace at randomness 0. */
function keep(
  policies: SpanSamplingImplementer,
  span: Partial<SpanAtStart> = {}
): boolean {
  const { traceId, name, kind, parent, traceState, attributes } = {
    traceId: POLICY_DROP_TRACE_ID,
    name: 'work',
    kind: SpanKind.INTERNAL,
    parent: LOCAL,
    traceState: undefined,
    attributes: undefined,
    ...span,
  };
  return policies.keep(traceId, name, kind, parent, traceState, attributes);
}

describe('policy span sampling', () => {
  beforeEach(() => {
    warnings = [];
  });

  it('should report the statuses of testdata/policy_decision_vectors.json', () => {
    const sets = policyVectors<DecisionVectors>('decision').policy_sets;
    for (const [name, set] of Object.entries(sets)) {
      if (set.malformed) continue;
      warnings = [];
      const statuses = new SpanSamplingImplementer(message =>
        warnings.push(message)
      ).applyPolicies(parsePolicySet(set.text));
      assert.deepStrictEqual(
        statuses.map(status => ({
          id: status.policyId,
          applied: status.applied,
        })),
        set.statuses,
        name
      );
      for (const status of statuses) {
        assert.strictEqual(
          typeof status.error,
          status.applied ? 'undefined' : 'string',
          `${name}: ${status.policyId}`
        );
        assert.notStrictEqual(status.error, '');
      }
      // One warning per policy that is not applied, in order.
      assert.deepStrictEqual(
        warnings,
        statuses
          .filter(status => !status.applied)
          .map(
            status =>
              `policy '${status.policyId}' is not applied: ${status.error}`
          ),
        name
      );
    }
  });

  it('should keep every span without policies', () => {
    const policies = new SpanSamplingImplementer();
    assert.ok(keep(policies));
    assert.ok(!policies.active);
  });

  it('should drop INTERNAL spans under a parent with the example policy', () => {
    const policies = new SpanSamplingImplementer();
    const statuses = policies.applyPolicies(
      parsePolicySet(readFileSync(EXAMPLE_POLICY, 'utf8'))
    );
    assert.deepStrictEqual(statuses, [
      { policyId: 'drop-internal-spans', applied: true },
    ]);
    assert.ok(policies.active);
    assert.ok(!keep(policies));
    assert.ok(!keep(policies, { kind: undefined }));
    assert.ok(keep(policies, { traceId: POLICY_KEEP_TRACE_ID }));
    assert.ok(keep(policies, { kind: SpanKind.SERVER }));
    assert.ok(keep(policies, { parent: undefined }));
    assert.ok(keep(policies, { parent: INVALID_SPAN_CONTEXT }));
    assert.ok(
      keep(policies, { traceState: createTraceState('muzzle=verbose') })
    );
    assert.ok(
      !keep(policies, { traceState: createTraceState('muzzle=quiet') })
    );
  });

  it('should let the most restrictive policy win', () => {
    const policies = implementer(
      sampling('all', 100),
      sampling('half', 50),
      sampling('none', 0, [KIND_IS_INTERNAL])
    );
    assert.ok(!keep(policies, { traceId: POLICY_KEEP_TRACE_ID }));
    assert.ok(
      keep(policies, { traceId: POLICY_KEEP_TRACE_ID, kind: SpanKind.CLIENT })
    );
    assert.ok(
      !keep(policies, { traceId: POLICY_DROP_TRACE_ID, kind: SpanKind.CLIENT })
    );
  });

  it('should leave a policy with an unsupported selector inert, with a warning', () => {
    for (const [target, selector] of [
      [{ resourceAttribute: { path: ['service.name'] } }, 'resource_attribute'],
      [{ scopeAttribute: { path: ['a'] } }, 'scope_attribute'],
      [{ scopeField: 'SCOPE_FIELD_NAME' }, 'scope_field'],
      [
        { recordField: 'SPAN_RECORD_FIELD_SPAN_ID' },
        'SPAN_RECORD_FIELD_SPAN_ID',
      ],
    ] as const) {
      warnings = [];
      const policies = new SpanSamplingImplementer(message =>
        warnings.push(message)
      );
      const statuses = policies.applyPolicies(
        parsePolicySet(
          JSON.stringify({
            policies: [
              sampling('by-resource', 0, [
                KIND_IS_INTERNAL,
                { target, predicate: EXISTS },
              ]),
            ],
          })
        )
      );
      const error = `matches[1].target: unsupported selector ${selector}`;
      assert.deepStrictEqual(statuses, [
        { policyId: 'by-resource', applied: false, error },
      ]);
      assert.ok(keep(policies));
      assert.ok(!policies.active);
      assert.deepStrictEqual(warnings, [
        `policy 'by-resource' is not applied: ${error}`,
      ]);
    }
  });

  it('should log a policy that is not applied through diag by default', () => {
    const [, messages] = withDiag(() =>
      new SpanSamplingImplementer().applyPolicies(
        parsePolicySet('{"policies": [{"id": "logs", "logFilter": {}}]}')
      )
    );
    assert.deepStrictEqual(messages, [
      "WARN otel-muzzle policy 'logs' is not applied: unsupported policy type: log_filter",
    ]);
  });

  it('should report a disabled policy as applied, and leave it out', () => {
    const policies = new SpanSamplingImplementer();
    const statuses = policies.applyPolicies(
      parsePolicySet(
        JSON.stringify({
          policies: [{ ...sampling('off', 0), enabled: false }],
        })
      )
    );
    assert.deepStrictEqual(statuses, [{ policyId: 'off', applied: true }]);
    assert.ok(keep(policies));
    assert.ok(!policies.active);
  });

  it('should not apply a policy that cannot be compiled', () => {
    // The parser never hands these over; a policy set built in code could.
    const bad: Policy = {
      id: 'bad',
      enabled: true,
      spanSampling: {
        matches: [
          {
            target: { kind: 'recordField', field: 'NAME' },
            predicate: { kind: 'regex', pattern: '(' },
            negate: false,
          },
        ],
        sampling: { percentage: 0, mode: 'EQUALIZING', precision: 4 },
      },
    };
    const policies = new SpanSamplingImplementer(message =>
      warnings.push(message)
    );
    const [status] = policies.applyPolicies([bad]);
    assert.strictEqual(status.applied, false);
    assert.match(status.error!, /^unsupported regex: /);
    assert.ok(keep(policies));
    assert.strictEqual(warnings.length, 1);
  });

  it('should keep the span when a policy throws, and warn once per policy', () => {
    const explosive = (): Attributes =>
      Object.defineProperty({}, 'a', {
        enumerable: true,
        get() {
          throw new Error('boom');
        },
      });
    const policies = implementer(
      sampling('fragile', 0, [{ target: attribute('a'), predicate: EXISTS }]),
      sampling('brittle', 0, [
        { target: attribute('a'), predicate: { equals: { intValue: 1 } } },
      ])
    );
    assert.ok(keep(policies, { attributes: explosive() }));
    assert.ok(keep(policies, { attributes: explosive() }));
    assert.deepStrictEqual(warnings, [
      "policy 'fragile' could not be evaluated and matches nothing until it can: Error: boom",
      "policy 'brittle' could not be evaluated and matches nothing until it can: Error: boom",
    ]);
    assert.ok(!keep(policies, { attributes: { a: 1 } }));
    // A new set starts over.
    policies.applyPolicies(
      parsePolicySet(
        JSON.stringify({
          policies: [
            sampling('fragile', 0, [
              { target: attribute('a'), predicate: EXISTS },
            ]),
          ],
        })
      )
    );
    assert.ok(keep(policies, { attributes: explosive() }));
    assert.strictEqual(warnings.length, 3);
  });

  it('should let another policy decide when one throws', () => {
    const policies = implementer(
      sampling('fragile', 100, [{ target: attribute('a'), predicate: EXISTS }]),
      sampling('none', 0)
    );
    const attributes = Object.defineProperty({}, 'a', {
      get() {
        throw new Error('boom');
      },
    });
    assert.ok(!keep(policies, { attributes }));
  });

  it('should ignore an arriving threshold above the randomness', () => {
    const policies = implementer(sampling('all', 100));
    assert.ok(keep(policies, { traceState: createTraceState('ot=th:8') }));
  });

  it('should never lower an arriving threshold', () => {
    // 50% arrives; an equalizing 10% policy raises the threshold to e666, and a 75% one leaves it at 8.
    const ten = implementer(sampling('ten', 10));
    const most = implementer(sampling('most', 75));
    const traceState = createTraceState('ot=th:8');
    const at = (randomness: string) => ({
      traceId: POLICY_DROP_TRACE_ID.slice(0, 18) + randomness,
      traceState,
    });
    assert.ok(!keep(ten, at('e665ffffffffff')));
    assert.ok(keep(ten, at('e6660000000000')));
    assert.ok(keep(most, at('80000000000000')));
    // Below the arriving threshold the span is inconsistent, and the policy's own 4 decides.
    assert.ok(keep(most, at('7fffffffffffff')));
    assert.ok(!keep(most, at('3fffffffffffff')));
  });

  it('should multiply the arriving probability in proportional mode', () => {
    // 10% of an arriving 50% is 5%: f3333 at precision 4 after one leading f. Of an arriving 25% it is 2.5%.
    const policies = implementer(
      sampling('ten', 10, [], { mode: 'SAMPLING_MODE_PROPORTIONAL' })
    );
    const at = (randomness: string, ot: string) => ({
      traceId: POLICY_DROP_TRACE_ID.slice(0, 18) + randomness,
      traceState: createTraceState(`ot=${ot}`),
    });
    assert.ok(!keep(policies, at('f3332fffffffff', 'th:8')));
    assert.ok(keep(policies, at('f3333000000000', 'th:8')));
    assert.ok(!keep(policies, at('f3333000000000', 'th:c')));
    assert.ok(keep(policies, at('f999a000000000', 'th:c')));
    assert.ok(keep(policies, at('f3333000000000', 'th:8')));
    // Without an arriving threshold it is the policy's own.
    assert.ok(keep(policies, at('e6660000000000', 'rv:e6660000000000')));
    assert.ok(!keep(policies, at('e6660000000000', 'rv:e665ffffffffff')));
  });

  it('should read rv instead of the trace id', () => {
    const policies = implementer(sampling('half', 50));
    assert.ok(
      !keep(policies, {
        traceId: POLICY_KEEP_TRACE_ID,
        traceState: createTraceState('ot=rv:00000000000000'),
      })
    );
    assert.ok(
      keep(policies, {
        traceId: POLICY_DROP_TRACE_ID,
        traceState: createTraceState('ot=rv:ffffffffffffff'),
      })
    );
  });

  it('should keep a span whose trace id gives no randomness, without throwing', () => {
    const policies = implementer(sampling('none', 0));
    for (const traceId of ['', 'abc', 'z'.repeat(32), 'F'.repeat(32)]) {
      assert.ok(keep(policies, { traceId }), traceId);
      assert.ok(
        !keep(policies, {
          traceId,
          traceState: createTraceState('ot=rv:ffffffffffffff'),
        }),
        traceId
      );
    }
    assert.ok(!keep(policies, { traceId: '0'.repeat(32) }));
  });

  it('should replace the whole set', () => {
    const policies = implementer(sampling('none', 0));
    assert.ok(!keep(policies, { traceId: POLICY_KEEP_TRACE_ID }));
    assert.deepStrictEqual(policies.applyPolicies([]), []);
    assert.ok(keep(policies, { traceId: POLICY_KEEP_TRACE_ID }));
    assert.ok(!policies.active);
  });

  describe('record fields', () => {
    const equals = (stringValue: string) => ({ equals: { stringValue } });

    it('should read KIND as its uppercase name, and a missing kind as INTERNAL', () => {
      for (const [kind, name] of [
        [SpanKind.INTERNAL, 'INTERNAL'],
        [SpanKind.SERVER, 'SERVER'],
        [SpanKind.CLIENT, 'CLIENT'],
        [SpanKind.PRODUCER, 'PRODUCER'],
        [SpanKind.CONSUMER, 'CONSUMER'],
        [undefined, 'INTERNAL'],
      ] as const) {
        assert.ok(!keep(dropWhen(KIND, equals(name)), { kind }), name);
        assert.ok(keep(dropWhen(KIND, equals('OTHER')), { kind }), name);
      }
      // A kind the API does not define has no name.
      assert.ok(!keep(dropWhen(KIND, EXISTS, true), { kind: 99 as SpanKind }));
    });

    it('should read TRACE_ID and PARENT_SPAN_ID as hexadecimal text', () => {
      const traceId = { recordField: 'SPAN_RECORD_FIELD_TRACE_ID' };
      const parentId = { recordField: 'SPAN_RECORD_FIELD_PARENT_SPAN_ID' };
      assert.ok(!keep(dropWhen(traceId, equals(POLICY_DROP_TRACE_ID))));
      assert.ok(keep(dropWhen(traceId, equals(POLICY_KEEP_TRACE_ID))));
      assert.ok(!keep(dropWhen(parentId, equals(LOCAL_SPAN_ID))));
      assert.ok(keep(dropWhen(parentId, equals(REMOTE_SPAN_ID))));
      for (const parent of [undefined, INVALID_SPAN_CONTEXT]) {
        assert.ok(keep(dropWhen(parentId, EXISTS), { parent }));
        assert.ok(!keep(dropWhen(parentId, EXISTS, true), { parent }));
        assert.ok(!keep(dropWhen(parentId, equals('')), { parent }));
      }
    });

    it('should read TRACE_STATE as the header, and as empty when there is none', () => {
      const traceState = { recordField: 'SPAN_RECORD_FIELD_TRACE_STATE' };
      const policies = dropWhen(traceState, equals('a=1,b=2'));
      assert.ok(!keep(policies, { traceState: createTraceState('a=1,b=2') }));
      assert.ok(keep(policies, { traceState: createTraceState('b=2,a=1') }));
      for (const empty of [undefined, createTraceState('')]) {
        assert.ok(keep(dropWhen(traceState, EXISTS), { traceState: empty }));
        assert.ok(
          !keep(dropWhen(traceState, equals('')), { traceState: empty })
        );
      }
    });

    it('should read a record field as text for every predicate', () => {
      assert.ok(!keep(dropWhen(NAME, { regex: '^wo' })));
      assert.ok(!keep(dropWhen(NAME, { contains: { stringValue: 'or' } })));
      assert.ok(
        keep(dropWhen(NAME, { contains: { intValue: 0 } }), { name: '0' })
      );
      assert.ok(
        keep(dropWhen(NAME, { equals: { intValue: 5 } }), { name: '5' })
      );
      assert.ok(keep(dropWhen(NAME, { gte: { intValue: 0 } }), { name: '5' }));
      assert.ok(
        !keep(dropWhen(NAME, { gte: { intValue: 0 } }, true), { name: '5' })
      );
      assert.ok(!keep(dropWhen(NAME, EXISTS, true), { name: '' }));
    });
  });

  describe('attributes', () => {
    const a = attribute('a');
    const dropped = (predicate: Json, value: unknown, negate = false) =>
      !keep(dropWhen(a, predicate, negate), {
        attributes: { a: value } as Attributes,
      });

    it('should read an absent attribute as matching only a negated matcher', () => {
      for (const attributes of [
        undefined,
        {},
        { b: 1 },
        { a: undefined },
        { a: null } as unknown as Attributes,
      ]) {
        for (const predicate of [
          EXISTS,
          { equals: { stringValue: '' } },
          { regex: '' },
          { lt: { doubleValue: 'Infinity' } },
          { contains: { stringValue: '' } },
        ]) {
          const label = JSON.stringify([attributes, predicate]);
          assert.ok(keep(dropWhen(a, predicate), { attributes }), label);
          assert.ok(!keep(dropWhen(a, predicate, true), { attributes }), label);
        }
      }
    });

    it('should read only the attributes the span was given itself', () => {
      // "toString" and "constructor" exist on every object without being attributes.
      for (const name of ['toString', 'constructor', '__proto__']) {
        const policies = dropWhen(attribute(name), EXISTS);
        assert.ok(keep(policies, { attributes: {} }), name);
        assert.ok(!keep(policies, { attributes: { [name]: 'x' } }), name);
      }
    });

    it('should read a path of several segments as absent from flat attributes', () => {
      const nested = dropWhen(attribute('http', 'route'), EXISTS);
      assert.ok(
        keep(nested, { attributes: { 'http.route': '/x', http: 'y' } })
      );
      assert.ok(keep(nested, { attributes: { http: ['route'] } }));
      // Each segment is a key of an object, should a caller ever pass one.
      assert.ok(
        !keep(nested, {
          attributes: { http: { route: '/x' } } as unknown as Attributes,
        })
      );
    });

    it('should compare integers and doubles in one numeric domain, exactly', () => {
      assert.ok(dropped({ equals: { intValue: '200' } }, 200));
      assert.ok(dropped({ equals: { doubleValue: 200 } }, 200));
      assert.ok(!dropped({ equals: { intValue: 200 } }, 200.5));
      assert.ok(!dropped({ equals: { intValue: 200 } }, '200'));
      assert.ok(!dropped({ equals: { intValue: 1 } }, true));
      assert.ok(!dropped({ equals: { boolValue: true } }, 1));
      assert.ok(!dropped({ equals: { doubleValue: 'NaN' } }, Number.NaN));
      // 2**53 and 2**53 + 1 are the same double, and different integers.
      assert.ok(dropped({ equals: { intValue: '9007199254740992' } }, 2 ** 53));
      assert.ok(
        !dropped({ equals: { intValue: '9007199254740993' } }, 2 ** 53)
      );
      assert.ok(dropped({ lt: { intValue: '9007199254740993' } }, 2 ** 53));
      assert.ok(!dropped({ gte: { intValue: '9007199254740993' } }, 2 ** 53));
      assert.ok(
        dropped({ gt: { intValue: '-9223372036854775808' } }, -1e300, true)
      );
      assert.ok(
        dropped({ gte: { intValue: '-9223372036854775808' } }, -(2 ** 63))
      );
      assert.ok(dropped({ lte: { intValue: '9223372036854775807' } }, 2 ** 62));
      assert.ok(
        !dropped({ lte: { intValue: '9223372036854775807' } }, 2 ** 63)
      );
      assert.ok(
        !dropped({ lte: { intValue: '9223372036854775807' } }, Number.NaN)
      );
      assert.ok(dropped({ lt: { doubleValue: 'Infinity' } }, 1.5e300));
      assert.ok(!dropped({ lt: { doubleValue: 'NaN' } }, 1));
      // A bigint is not an attribute type, and still compares as the integer it is.
      assert.ok(
        dropped({ equals: { intValue: '9007199254740993' } }, 9007199254740993n)
      );
      assert.ok(dropped({ equals: { intValue: 7 } }, 7n));
      assert.ok(dropped({ gt: { doubleValue: 6.5 } }, 7n));
    });

    it('should match contains on arrays and strings', () => {
      const err = { contains: { stringValue: 'err' } };
      assert.ok(dropped(err, 'an error'));
      assert.ok(dropped(err, ['ok', 'err']));
      assert.ok(!dropped(err, ['an error']));
      assert.ok(!dropped(err, 5));
      assert.ok(!dropped(err, true));
      assert.ok(dropped({ contains: { intValue: 7 } }, [1, 7]));
      assert.ok(dropped({ contains: { intValue: 7 } }, [null, 7.0]));
      assert.ok(!dropped({ contains: { intValue: 7 } }, '7'));
      assert.ok(!dropped({ contains: { intValue: 7 } }, ['7']));
      assert.ok(dropped({ contains: { boolValue: false } }, [true, false]));
      assert.ok(!dropped({ contains: { boolValue: false } }, [0]));
    });

    it('should match bytes only against bytes', () => {
      const x = { bytesValue: 'eA==' };
      assert.ok(!dropped({ equals: x }, 'x'));
      assert.ok(!dropped({ equals: x }, 'eA=='));
      assert.ok(!dropped({ contains: x }, 'x'));
      // Bytes are not a start attribute type; a caller that passes them gets them compared.
      assert.ok(dropped({ equals: x }, new Uint8Array([0x78])));
      assert.ok(!dropped({ equals: x }, new Uint8Array([0x78, 0x79])));
      assert.ok(dropped({ contains: x }, new Uint8Array([0x77, 0x78, 0x79])));
      assert.ok(!dropped({ contains: x }, new Uint8Array([0x77, 0x79])));
      assert.ok(dropped({ contains: x }, [new Uint8Array([0x78])]));
      assert.ok(
        !dropped({ contains: { stringValue: 'x' } }, new Uint8Array([0x78]))
      );
    });

    it('should match a regex only against a string', () => {
      assert.ok(dropped({ regex: '^5[0-9]{2}$' }, '503'));
      assert.ok(!dropped({ regex: '^5[0-9]{2}$' }, 503));
      assert.ok(!dropped({ regex: '^5[0-9]{2}$' }, ['503']));
      assert.ok(dropped({ regex: '^ok$' }, 5, true));
    });
  });
});
