// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import { readFileSync } from 'node:fs';
import {
  isInvalidPolicy,
  type InvalidPolicy,
  type Policy,
  type Scalar,
  type SpanSamplingPolicy,
} from '../src/policy/model';
import { parsePolicySet, PolicyDocumentError } from '../src/policy/parser';
import { EXAMPLE_POLICY, policyVectors } from './helpers';

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

const KIND = { recordField: 'SPAN_RECORD_FIELD_KIND' };
const EXISTS = { exists: {} };

/** Parses one policy with the id "p" and the given span sampling definition. */
function parseOne(
  spanSampling: unknown,
  envelope: Json = {}
): Policy | InvalidPolicy {
  const entries = parsePolicySet(
    JSON.stringify({ policies: [{ id: 'p', ...envelope, spanSampling }] })
  );
  assert.strictEqual(entries.length, 1);
  return entries[0];
}

/** The span sampling definition of a policy that must be valid. */
function validOne(spanSampling: unknown): SpanSamplingPolicy {
  const entry = parseOne(spanSampling);
  if (isInvalidPolicy(entry)) assert.fail(entry.error);
  assert.ok(entry.spanSampling !== undefined);
  return entry.spanSampling;
}

/** The error of a policy that must be invalid. */
function errorOf(spanSampling: unknown): string {
  const entry = parseOne(spanSampling);
  assert.ok(isInvalidPolicy(entry), JSON.stringify(spanSampling));
  return entry.error;
}

/** A definition with one matcher on KIND and the given predicate. */
const withPredicate = (predicate: unknown) => ({
  matches: [{ target: KIND, predicate }],
  sampling: { percentage: 0 },
});

/** The value an `equals` predicate parses to. */
function scalar(value: Json): Scalar {
  const { predicate } = validOne(withPredicate({ equals: value })).matches[0];
  assert.ok(predicate.kind === 'equals');
  return predicate.value;
}

describe('policy parser', () => {
  it('should read the example policy', () => {
    const entries = parsePolicySet(readFileSync(EXAMPLE_POLICY, 'utf8'));
    assert.deepStrictEqual(entries, [
      {
        id: 'drop-internal-spans',
        enabled: true,
        spanSampling: {
          matches: [
            {
              target: { kind: 'recordField', field: 'KIND' },
              predicate: { kind: 'equals', value: 'INTERNAL' },
              negate: false,
            },
            {
              target: { kind: 'recordField', field: 'PARENT_SPAN_ID' },
              predicate: { kind: 'exists' },
              negate: false,
            },
            {
              target: { kind: 'recordField', field: 'TRACE_STATE' },
              predicate: {
                kind: 'regex',
                pattern: '(^|,)\\s*muzzle=verbose\\s*(,|$)',
              },
              negate: true,
            },
          ],
          sampling: { percentage: 10, mode: 'PROPORTIONAL', precision: 4 },
        },
      },
    ]);
  });

  it('should accept lowerCamelCase and snake_case names', () => {
    const camel = parseOne({
      matches: [
        {
          target: { spanAttribute: { path: ['a'] } },
          predicate: { gte: { intValue: '5' } },
        },
      ],
      sampling: { percentage: 10, samplingPrecision: 5 },
    });
    const [snake] = parsePolicySet(
      JSON.stringify({
        policies: [
          {
            id: 'p',
            span_sampling: {
              matches: [
                {
                  target: { span_attribute: { path: ['a'] } },
                  predicate: { gte: { int_value: 5 } },
                },
              ],
              sampling: { percentage: '10', sampling_precision: '5' },
            },
          },
        ],
      })
    );
    assert.deepStrictEqual(camel, snake);
    assert.deepStrictEqual(camel, {
      id: 'p',
      enabled: true,
      spanSampling: {
        matches: [
          {
            target: { kind: 'spanAttribute', path: ['a'] },
            predicate: { kind: 'compare', op: 'gte', value: 5 },
            negate: false,
          },
        ],
        sampling: { percentage: 10, mode: 'EQUALIZING', precision: 5 },
      },
    });
  });

  it('should read every scalar type', () => {
    assert.strictEqual(scalar({ stringValue: 'x' }), 'x');
    assert.strictEqual(scalar({ intValue: 200.0 }), 200);
    assert.strictEqual(scalar({ intValue: '200' }), 200);
    assert.strictEqual(scalar({ intValue: '-0' }), 0);
    assert.strictEqual(scalar({ boolValue: false }), false);
    assert.strictEqual(scalar({ doubleValue: '-Infinity' }), -Infinity);
    assert.strictEqual(scalar({ doubleValue: 'Infinity' }), Infinity);
    assert.ok(Number.isNaN(scalar({ doubleValue: 'NaN' })));
    assert.strictEqual(scalar({ doubleValue: 3 }), 3);
    assert.strictEqual(scalar({ doubleValue: '-.5e1' }), -5);
    assert.strictEqual(scalar({ doubleValue: '1e999' }), Infinity);
    assert.deepStrictEqual(scalar({ bytesValue: 'eA==' }), Buffer.from('x'));
    assert.deepStrictEqual(scalar({ bytesValue: '' }), Buffer.alloc(0));
  });

  it('should keep an int64 that a number cannot hold exactly as a bigint', () => {
    assert.strictEqual(
      scalar({ intValue: '9007199254740991' }),
      Number.MAX_SAFE_INTEGER
    );
    assert.strictEqual(
      scalar({ intValue: '9007199254740992' }),
      9007199254740992n
    );
    assert.strictEqual(
      scalar({ intValue: '-9223372036854775808' }),
      -(2n ** 63n)
    );
    assert.strictEqual(
      scalar({ intValue: '9223372036854775807' }),
      2n ** 63n - 1n
    );
    const { predicate } = validOne(
      withPredicate({ lt: { intValue: '-9007199254740993' } })
    ).matches[0];
    assert.deepStrictEqual(predicate, {
      kind: 'compare',
      op: 'lt',
      value: -9007199254740993n,
    });
  });

  it('should reject a number that Protobuf JSON does not allow', () => {
    for (const equals of [
      { intValue: 1.5 },
      { intValue: '1.0' },
      { intValue: '01' },
      { intValue: '+1' },
      { intValue: ' 1' },
      { intValue: '' },
      { intValue: '0x10' },
      { intValue: '1e3' },
      { intValue: '9223372036854775808' },
      { intValue: '-9223372036854775809' },
      { intValue: 9007199254740992 },
      { intValue: -9007199254740992 },
      { intValue: 1e300 },
      { intValue: true },
      { intValue: [1] },
      { doubleValue: 'half' },
      { doubleValue: '' },
      { doubleValue: ' 1' },
      { doubleValue: '1 ' },
      { doubleValue: '1.' },
      { doubleValue: '+1' },
      { doubleValue: '0x10' },
      { doubleValue: 'infinity' },
      { doubleValue: 'nan' },
      { doubleValue: '-NaN' },
      { doubleValue: '1_000' },
      { doubleValue: true },
      { doubleValue: [1] },
      { bytesValue: '!!' },
      { bytesValue: 'eA' },
      { bytesValue: 'eA=' },
      { bytesValue: 'eA==\n' },
      { bytesValue: 5 },
      { boolValue: 'true' },
      { boolValue: 0 },
      { stringValue: 5 },
    ]) {
      assert.match(
        errorOf(withPredicate({ equals })),
        /matches\[0\]\.predicate\.equals\.\w+Value: must be /,
        JSON.stringify(equals)
      );
    }
  });

  it('should read the other predicates and targets', () => {
    const { matches, sampling } = validOne({
      matches: [
        { target: KIND, predicate: { contains: { stringValue: 'ERN' } } },
        {
          target: { resourceAttribute: { path: ['service.name'] } },
          predicate: EXISTS,
        },
        {
          target: { scopeField: 'SCOPE_FIELD_NAME' },
          predicate: { lt: { doubleValue: 1.5 } },
        },
        {
          target: { scope_attribute: { path: ['a', 'b'] } },
          predicate: EXISTS,
        },
        {
          target: { recordField: 'SPAN_RECORD_FIELD_SPAN_ID' },
          predicate: EXISTS,
          negate: true,
        },
      ],
      sampling: { percentage: 100, mode: 'SAMPLING_MODE_UNSPECIFIED' },
    });
    assert.deepStrictEqual(matches, [
      {
        target: { kind: 'recordField', field: 'KIND' },
        predicate: { kind: 'contains', value: 'ERN' },
        negate: false,
      },
      {
        target: { kind: 'unsupported', selector: 'resource_attribute' },
        predicate: { kind: 'exists' },
        negate: false,
      },
      {
        target: { kind: 'unsupported', selector: 'scope_field' },
        predicate: { kind: 'compare', op: 'lt', value: 1.5 },
        negate: false,
      },
      {
        target: { kind: 'unsupported', selector: 'scope_attribute' },
        predicate: { kind: 'exists' },
        negate: false,
      },
      {
        target: { kind: 'recordField', field: 'SPAN_ID' },
        predicate: { kind: 'exists' },
        negate: true,
      },
    ]);
    assert.deepStrictEqual(sampling, {
      percentage: 100,
      mode: 'EQUALIZING',
      precision: 4,
    });
  });

  it('should read every sampling mode by name, and nothing else', () => {
    const mode = (name: unknown) => ({
      sampling: { percentage: 5, mode: name },
    });
    for (const [name, expected] of [
      ['SAMPLING_MODE_UNSPECIFIED', 'EQUALIZING'],
      ['SAMPLING_MODE_EQUALIZING', 'EQUALIZING'],
      ['SAMPLING_MODE_PROPORTIONAL', 'PROPORTIONAL'],
      [null, 'EQUALIZING'],
    ] as const) {
      assert.strictEqual(validOne(mode(name)).sampling.mode, expected);
    }
    for (const name of [
      'PROPORTIONAL',
      'sampling_mode_proportional',
      2,
      'toString',
      'constructor',
      '',
    ]) {
      assert.match(
        errorOf(mode(name)),
        /sampling\.mode: unknown sampling mode/
      );
    }
  });

  it('should allow matches to be empty or missing', () => {
    for (const body of [
      { matches: [], sampling: { percentage: 5 } },
      { sampling: { percentage: 5 } },
      { matches: null, sampling: { percentage: 5 } },
    ]) {
      assert.deepStrictEqual(validOne(body).matches, []);
    }
  });

  it('should read a null as an unset field', () => {
    const { matches, sampling } = validOne({
      matches: [
        {
          target: {
            recordField: 'SPAN_RECORD_FIELD_NAME',
            spanAttribute: null,
          },
          predicate: { exists: {}, regex: null },
          negate: null,
        },
      ],
      sampling: { percentage: '2.5', mode: null, samplingPrecision: null },
    });
    assert.deepStrictEqual(matches[0], {
      target: { kind: 'recordField', field: 'NAME' },
      predicate: { kind: 'exists' },
      negate: false,
    });
    assert.deepStrictEqual(sampling, {
      percentage: 2.5,
      mode: 'EQUALIZING',
      precision: 4,
    });
    const entry = parseOne({ sampling: { percentage: 1 } }, { enabled: null });
    assert.ok(!isInvalidPolicy(entry) && entry.enabled);
    assert.match(
      errorOf({ sampling: { percentage: null } }),
      /sampling\.percentage: must be set/
    );
  });

  it('should skip an invalid policy and parse the rest', () => {
    const [bad, good, junk] = parsePolicySet(
      JSON.stringify({
        policies: [
          { id: 'bad', spanSampling: { sampling: { percentage: 150 } } },
          { id: 'good', spanSampling: { sampling: { percentage: 50 } } },
          'not a policy',
        ],
      })
    );
    assert.ok(isInvalidPolicy(bad) && bad.id === 'bad');
    assert.match(bad.error, /percentage/);
    assert.ok(!isInvalidPolicy(good) && good.id === 'good');
    assert.ok(isInvalidPolicy(junk) && junk.id === '');
  });

  it('should make every copy of a duplicate id invalid', () => {
    const policy = {
      id: 'twice',
      spanSampling: { sampling: { percentage: 50 } },
    };
    const [first, second, other, off] = parsePolicySet(
      JSON.stringify({
        policies: [
          policy,
          policy,
          { ...policy, id: 'once' },
          { ...policy, enabled: false },
        ],
      })
    );
    assert.ok(isInvalidPolicy(first) && isInvalidPolicy(second));
    assert.strictEqual(first.error, "policies[0].id: duplicate id 'twice'");
    assert.strictEqual(second.error, "policies[1].id: duplicate id 'twice'");
    assert.deepStrictEqual([first.id, second.id], ['twice', 'twice']);
    assert.ok(!isInvalidPolicy(other));
    assert.deepStrictEqual(off, { id: 'twice', enabled: false });
  });

  it('should not count a disabled policy for duplicate ids', () => {
    const policy = { id: 'p', spanSampling: { sampling: { percentage: 50 } } };
    const entries = parsePolicySet(
      JSON.stringify({ policies: [policy, { ...policy, enabled: false }] })
    );
    assert.deepStrictEqual(entries.map(isInvalidPolicy), [false, false]);
  });

  it('should not validate a disabled policy', () => {
    assert.deepStrictEqual(
      parseOne({ sampling: { percentage: 999 } }, { enabled: false }),
      { id: 'p', enabled: false }
    );
  });

  it('should name another policy type', () => {
    for (const [key, name] of [
      ['log_filter', 'log_filter'],
      ['logFilter', 'log_filter'],
      ['logTransform', 'log_transform'],
      ['metric_filter', 'metric_filter'],
    ]) {
      assert.deepStrictEqual(
        parsePolicySet(
          JSON.stringify({ policies: [{ id: 'logs', [key]: {} }] })
        ),
        [{ id: 'logs', enabled: true, unsupported: name }]
      );
    }
  });

  it('should name the location of an error', () => {
    assert.strictEqual(
      errorOf({
        matches: [{ target: KIND, predicate: EXISTS }, { predicate: EXISTS }],
        sampling: { percentage: 0 },
      }),
      'policies[0].spanSampling.matches[1].target: must be set'
    );
    assert.strictEqual(
      errorOf({ sampling: { percentage: 'ten' } }),
      'policies[0].spanSampling.sampling.percentage: must be a number'
    );
    assert.match(
      errorOf(withPredicate({ regex: '(' })),
      /^policies\[0\]\.spanSampling\.matches\[0\]\.predicate\.regex: unsupported regex: /
    );
    assert.strictEqual(
      errorOf({
        sampling: {
          percentage: 0,
          samplingPrecision: 4,
          sampling_precision: 4,
        },
      }),
      'policies[0].spanSampling.sampling: both samplingPrecision and sampling_precision are set'
    );
    assert.strictEqual(
      errorOf({ sampling: { percentage: 0, hashSeed: 1 } }),
      "policies[0].spanSampling.sampling: unknown key 'hashSeed'"
    );
    assert.strictEqual(
      errorOf({
        matches: [
          { target: { spanAttribute: { path: [] } }, predicate: EXISTS },
        ],
        sampling: { percentage: 0 },
      }),
      'policies[0].spanSampling.matches[0].target.span_attribute.path: must be a non-empty list of strings'
    );
    assert.strictEqual(
      errorOf(withPredicate({})),
      'policies[0].spanSampling.matches[0].predicate: exactly one of exists, equals, regex, gt, gte, lt, lte, ' +
        'contains must be set'
    );
    assert.strictEqual(
      errorOf(withPredicate({ gt: { stringValue: 'a' } })),
      "policies[0].spanSampling.matches[0].predicate.gt: unknown key 'stringValue'"
    );
    assert.strictEqual(
      errorOf({
        matches: [{ target: { scopeField: 5 }, predicate: EXISTS }],
        sampling: { percentage: 0 },
      }),
      'policies[0].spanSampling.matches[0].target.scopeField: must be a scope field name'
    );
    assert.strictEqual(
      errorOf({
        matches: [{ target: KIND, predicate: EXISTS, negate: 'yes' }],
        sampling: { percentage: 0 },
      }),
      'policies[0].spanSampling.matches[0].negate: must be a boolean'
    );
    assert.deepStrictEqual(
      parsePolicySet('{"policies": [{"id": "p", "enabled": "no"}, 17]}'),
      [
        { id: 'p', error: 'policies[0].enabled: must be a boolean' },
        { id: '', error: 'policies[1]: a policy must be an object' },
      ]
    );
  });

  it('should reject an unknown key anywhere inside a span sampling policy', () => {
    const probe = { target: KIND, predicate: EXISTS };
    for (const body of [
      { sampling: { percentage: 0 }, keep: true },
      { sampling: { percentage: 0, hashSeed: 1 } },
      {
        matches: [{ ...probe, caseInsensitive: true }],
        sampling: { percentage: 0 },
      },
      {
        matches: [{ ...probe, target: { ...KIND, field: 1 } }],
        sampling: { percentage: 0 },
      },
      {
        matches: [
          { ...probe, target: { spanAttribute: { path: ['a'], key: 'a' } } },
        ],
        sampling: { percentage: 0 },
      },
      {
        matches: [{ ...probe, predicate: { glob: 'INT*' } }],
        sampling: { percentage: 0 },
      },
      {
        matches: [{ ...probe, predicate: { exists: { value: true } } }],
        sampling: { percentage: 0 },
      },
      {
        matches: [
          {
            ...probe,
            predicate: { equals: { stringValue: 'a', caseInsensitive: true } },
          },
        ],
        sampling: { percentage: 0 },
      },
      {
        matches: [{ ...probe, predicate: { gt: { intValue: 1, unit: 'ms' } } }],
        sampling: { percentage: 0 },
      },
    ]) {
      assert.match(errorOf(body), /: unknown key '\w+'$/, JSON.stringify(body));
    }
  });

  it('should ignore an unknown key in the policy envelope and at the top level', () => {
    const entries = parsePolicySet(
      JSON.stringify({
        futureTopLevel: {},
        policies: [
          {
            id: 'p',
            name: 'n',
            labels: [{ key: 'team', value: { stringValue: 'x' } }],
            futureField: 1,
            spanSampling: { sampling: { percentage: 1 } },
          },
        ],
      })
    );
    assert.deepStrictEqual(entries.map(isInvalidPolicy), [false]);
  });

  it('should read only the keys a document has itself', () => {
    // JSON.parse makes "__proto__" an ordinary key, and names like "constructor" exist on every object.
    assert.strictEqual(
      errorOf(JSON.parse('{"sampling": {"percentage": 0}, "__proto__": {}}')),
      "policies[0].spanSampling: unknown key '__proto__'"
    );
    for (const field of [
      'constructor',
      'toString',
      '__proto__',
      'UNSPECIFIED',
    ]) {
      assert.match(
        errorOf({
          matches: [
            {
              target: { recordField: `SPAN_RECORD_FIELD_${field}` },
              predicate: EXISTS,
            },
          ],
          sampling: { percentage: 0 },
        }),
        /target\.recordField: unknown span record field/,
        field
      );
    }
    const [inherited] = parsePolicySet(
      '{"policies": [{"__proto__": {"id": "p", "enabled": false}}]}'
    );
    assert.deepStrictEqual(inherited, {
      id: '',
      error: 'policies[0].id: a policy needs a non-empty string id',
    });
  });

  it('should check the percentage and the precision', () => {
    for (const percentage of [0, 100, '100.0', '1e-9', 33.3]) {
      assert.strictEqual(
        validOne({ sampling: { percentage } }).sampling.percentage,
        Number(percentage)
      );
    }
    for (const percentage of [
      150,
      -0.5,
      'NaN',
      'Infinity',
      100.00001,
      true,
      [],
      {},
    ]) {
      assert.match(
        errorOf({ sampling: { percentage } }),
        /sampling\.percentage: must be (within 0 to 100|a number)$/,
        JSON.stringify(percentage)
      );
    }
    for (const samplingPrecision of [1, 14, '7', 4.0]) {
      assert.strictEqual(
        validOne({ sampling: { percentage: 0, samplingPrecision } }).sampling
          .precision,
        Number(samplingPrecision)
      );
    }
    for (const samplingPrecision of [
      0,
      15,
      2.5,
      -1,
      '99999999999999999999',
      '9007199254740993',
      'four',
      true,
    ]) {
      assert.match(
        errorOf({ sampling: { percentage: 0, samplingPrecision } }),
        /sampling\.samplingPrecision: must be (within 1 to 14|a 64-bit integer)$/,
        JSON.stringify(samplingPrecision)
      );
    }
  });

  it('should throw for a malformed document', () => {
    for (const text of [
      '',
      '{',
      '[]',
      'null',
      '"x"',
      '5',
      '{"policies": {}}',
      '{"policies": "x"}',
      '{"policies": NaN}',
      '\ufeff{}',
    ]) {
      assert.throws(() => parsePolicySet(text), PolicyDocumentError, text);
    }
    assert.throws(
      () => parsePolicySet('{'),
      /^PolicyDocumentError: not valid JSON: /
    );
    assert.throws(
      () => parsePolicySet('[]'),
      /^PolicyDocumentError: a policy set must be a JSON object$/
    );
    assert.throws(
      () => parsePolicySet('{"policies": {}}'),
      /^PolicyDocumentError: policies must be a list$/
    );
  });

  it('should read an empty document as an empty set', () => {
    for (const text of [
      '{}',
      '{"policies": []}',
      '{"policies": null}',
      '{"other": 1}',
    ]) {
      assert.deepStrictEqual(parsePolicySet(text), [], text);
    }
  });

  it('should parse every policy set of testdata/policy_decision_vectors.json', () => {
    const sets = policyVectors<DecisionVectors>('decision').policy_sets;
    assert.ok(Object.keys(sets).length >= 19);
    for (const [name, set] of Object.entries(sets)) {
      if (set.malformed) {
        assert.throws(
          () => parsePolicySet(set.text),
          PolicyDocumentError,
          name
        );
      } else {
        assert.deepStrictEqual(
          parsePolicySet(set.text).map(entry => entry.id),
          set.statuses.map(status => status.id),
          name
        );
      }
    }
  });
});
