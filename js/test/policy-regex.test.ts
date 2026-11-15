// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import { compileRegex } from '../src/policy/regex';
import { policyVectors } from './helpers';

interface RegexVectors {
  valid: Array<{ pattern: string; match: string[]; no_match: string[] }>;
  invalid: Array<{ pattern: string }>;
}

describe('policy regex', () => {
  it('should match as every valid case of testdata/policy_regex_vectors.json says', () => {
    const { valid } = policyVectors<RegexVectors>('regex');
    assert.ok(valid.length > 40, `only ${valid.length} cases`);
    for (const c of valid) {
      const regex = compileRegex(c.pattern);
      for (const text of c.match) {
        assert.ok(regex.test(text), JSON.stringify([c.pattern, text]));
      }
      for (const text of c.no_match) {
        assert.ok(!regex.test(text), JSON.stringify([c.pattern, text]));
      }
    }
  });

  it('should reject every invalid case of testdata/policy_regex_vectors.json', () => {
    const { invalid } = policyVectors<RegexVectors>('regex');
    assert.ok(invalid.length > 80, `only ${invalid.length} cases`);
    for (const c of invalid) {
      assert.throws(
        () => compileRegex(c.pattern),
        /^Error: unsupported regex: /,
        JSON.stringify(c.pattern)
      );
    }
  });

  it('should not let $ match before a trailing newline', () => {
    assert.ok(!compileRegex('a$').test('a\n'));
  });

  it('should reject backreferences and lookaround', () => {
    for (const pattern of ['(a)\\1', '(?=a)', '(?<!a)b']) {
      assert.throws(() => compileRegex(pattern), /unsupported regex/, pattern);
    }
  });

  it('should honour a leading flag group', () => {
    assert.ok(compileRegex('(?i)^get').test('GET /'));
    assert.ok(!compileRegex('^get').test('GET /'));
  });

  it('should name the position in the error', () => {
    assert.throws(() => compileRegex('(a)\\1'), /escape at 3$/);
    assert.throws(() => compileRegex('ab[cd'), /character class at 2$/);
  });

  it('should turn the leading flag group into RegExp flags, and never use u, g or y', () => {
    assert.strictEqual(compileRegex('abc').flags, '');
    assert.strictEqual(compileRegex('(?i)abc').flags, 'i');
    assert.strictEqual(compileRegex('(?smi)abc').flags, 'ims');
    assert.strictEqual(compileRegex('(?ssii)abc').flags, 'is');
    // Without the g and y flags a RegExp keeps no position between calls.
    const regex = compileRegex('b');
    assert.deepStrictEqual(
      [regex.test('abab'), regex.test('abab'), regex.test('b')],
      [true, true, true]
    );
  });

  it('should translate what a RegExp would read differently from RE2', () => {
    // "." is every character but "\n", \s is [\t\n\f\r ], and punctuation is written as \xHH.
    assert.strictEqual(
      compileRegex('^a.b\\s\\S[\\s,-]\\.c/$').source,
      '^a[^\\n]b[\\t\\n\\f\\r\\x20][^\\t\\n\\f\\r\\x20][\\t\\n\\f\\r\\x20\\x2c\\x2d]\\x2ec\\x2f$'
    );
    assert.strictEqual(compileRegex('(?s)a.b').source, 'a.b');
    assert.strictEqual(
      compileRegex('\\x4A[\\x4a-\\x4F]').source,
      '\\x4a[\\x4a-\\x4f]'
    );
  });

  it('should accept the patterns a RegExp would read as syntax of its own', () => {
    // Annex B and ES2018 syntax that the allowlist never passes through unescaped.
    assert.ok(compileRegex('^\\<\\=\\!\\:$').test('<=!:'));
    assert.ok(compileRegex('^a\\{1\\}$').test('a{1}'));
    assert.ok(!compileRegex('^a\\{1\\}$').test('a'));
    assert.ok(compileRegex('^[\\^a]$').test('^'));
    assert.ok(compileRegex('^[a\\-c]$').test('-'));
    assert.ok(!compileRegex('^[a\\-c]$').test('b'));
  });

  it('should reject a character class that ends in the middle of a range', () => {
    for (const pattern of ['[a-', '[^a-', 'x[\\x41-']) {
      assert.throws(
        () => compileRegex(pattern),
        /unterminated character class at [01]$/,
        pattern
      );
    }
  });

  it('should reject a repetition count that is not a small number', () => {
    for (const pattern of [
      'a{99999999999999999999}',
      'a{1,99999999999999999999}',
    ]) {
      assert.throws(() => compileRegex(pattern), /repetition count/, pattern);
    }
    assert.ok(compileRegex('^a{01}$').test('a'));
    assert.ok(compileRegex('^a{1000}$').test('a'.repeat(1000)));
  });
});
