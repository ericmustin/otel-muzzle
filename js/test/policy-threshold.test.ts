// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import {
  eraseTh,
  KEEP_NOTHING,
  MAX_THRESHOLD,
  parseOt,
  proportional,
  randomness,
  threshold,
  thresholdText,
} from '../src/policy/threshold';
import { policyVectors } from './helpers';

interface ThresholdVectors {
  cases: Array<{
    percentage: number;
    precision: number;
    threshold: string;
    th: string | null;
  }>;
  proportional: Array<{
    arriving: string;
    percentage: number;
    precision: number;
    threshold: string;
  }>;
}

interface TracestateVectors {
  cases: Array<{
    trace_id: string;
    ot: string | null;
    rv: string | null;
    th: string | null;
    randomness: string;
  }>;
}

/** A threshold as the vectors write it: 14 hexadecimal digits, or "100000000000000" for 2**56. */
const hex = (value: bigint) => value.toString(16).padStart(14, '0');

describe('policy threshold', () => {
  it('should match every case of testdata/policy_threshold_vectors.json', () => {
    const { cases } = policyVectors<ThresholdVectors>('threshold');
    assert.ok(cases.length > 300, `only ${cases.length} cases`);
    for (const c of cases) {
      const got = threshold(c.percentage / 100, c.precision);
      assert.strictEqual(hex(got), c.threshold, JSON.stringify(c));
    }
  });

  it('should match every proportional case of testdata/policy_threshold_vectors.json', () => {
    const cases = policyVectors<ThresholdVectors>('threshold').proportional;
    assert.ok(cases.length > 200, `only ${cases.length} cases`);
    for (const c of cases) {
      const got = proportional(
        BigInt(`0x${c.arriving}`),
        c.percentage / 100,
        c.precision
      );
      assert.strictEqual(hex(got), c.threshold, JSON.stringify(c));
    }
  });

  it('should keep nothing at zero and everything at one', () => {
    assert.strictEqual(threshold(0, 4), MAX_THRESHOLD);
    assert.strictEqual(MAX_THRESHOLD, 2n ** 56n);
    assert.strictEqual(threshold(1, 4), 0n);
    assert.strictEqual(threshold(-0.5, 4), MAX_THRESHOLD);
    assert.strictEqual(threshold(1.5, 4), 0n);
  });

  it('should give e666 for ten percent at the default precision', () => {
    assert.strictEqual(threshold(0.1, 4), 0xe6660000000000n);
    assert.strictEqual(threshold(0.01, 4), 0xfd70a000000000n);
  });

  it('should read the binary exponent exactly at powers of two', () => {
    // 2**-k is 0.5 * 2**(1 - k). At precision 1 its threshold, 2**56 - 2**(56 - k), needs every digit that
    // exponent allows: an exponent one too high would round the last digit away.
    for (let k = 1; k <= 56; k++) {
      assert.strictEqual(
        threshold(2 ** -k, 1),
        MAX_THRESHOLD - 2n ** BigInt(56 - k),
        `2**-${k}`
      );
    }
    assert.strictEqual(hex(threshold(2 ** -4 - 2 ** -8, 1)), 'f1000000000000');
    assert.strictEqual(hex(threshold(2 ** -4 + 2 ** -7, 1)), 'f0000000000000');
  });

  it('should round a probability below 2**-56 half up, down to subnormal numbers', () => {
    assert.strictEqual(threshold(2 ** -57, 1), MAX_THRESHOLD - 1n);
    assert.strictEqual(threshold(2 ** -58, 1), MAX_THRESHOLD);
    assert.strictEqual(threshold(1e-310, 14), MAX_THRESHOLD);
    assert.strictEqual(threshold(5e-324, 1), MAX_THRESHOLD);
  });

  it('should write a threshold as text that compares the way the numbers do', () => {
    assert.strictEqual(thresholdText(0n), '00000000000000');
    assert.strictEqual(thresholdText(0xe6660000000000n), 'e6660000000000');
    assert.strictEqual(thresholdText(MAX_THRESHOLD), KEEP_NOTHING);
    const ordered = [0n, 1n, 0x9n << 52n, 0xan << 52n, MAX_THRESHOLD - 1n];
    const texts = [...ordered.map(thresholdText), KEEP_NOTHING];
    assert.deepStrictEqual([...texts].sort(), texts);
    assert.ok(KEEP_NOTHING > 'ffffffffffffff');
  });
});

describe('policy tracestate', () => {
  it('should read the ot member as every case of testdata/policy_tracestate_vectors.json says', () => {
    const { cases } = policyVectors<TracestateVectors>('tracestate');
    assert.ok(cases.length > 40, `only ${cases.length} cases`);
    for (const c of cases) {
      const [rv, th] = parseOt(c.ot ?? undefined);
      const label = JSON.stringify(c);
      assert.strictEqual(rv ?? null, c.rv, label);
      assert.strictEqual(th ?? null, c.th, label);
      assert.strictEqual(randomness(c.trace_id, rv), c.randomness, label);
    }
  });

  it('should prefer rv over the trace id', () => {
    assert.strictEqual(
      randomness('f'.repeat(32), '00000000000000'),
      '00000000000000'
    );
    assert.strictEqual(
      randomness('4bf92f3577b34da6a3ce929d0e0e4736', undefined),
      'ce929d0e0e4736'
    );
  });

  it('should have no randomness for a trace id that is not 32 lowercase hexadecimal characters', () => {
    for (const traceId of [
      '',
      'abc',
      `${'0'.repeat(18)}zzzzzzzzzzzzzz`,
      'F'.repeat(32),
      'f'.repeat(33),
      `${'f'.repeat(31)}\n`,
    ]) {
      assert.strictEqual(randomness(traceId, undefined), undefined, traceId);
      assert.strictEqual(
        randomness(traceId, 'ffffffffffffff'),
        'ffffffffffffff'
      );
    }
    assert.strictEqual(randomness('0'.repeat(32), undefined), '00000000000000');
  });

  it('should remove only th from the ot member', () => {
    for (const [ot, expected] of [
      ['th:8', undefined],
      ['th:8;rv:00000000000000', 'rv:00000000000000'],
      ['ext:1;th:8;x:2', 'ext:1;x:2'],
      ['rv:1', 'rv:1'],
    ] as const) {
      assert.strictEqual(eraseTh(ot), expected, ot);
    }
  });
});
