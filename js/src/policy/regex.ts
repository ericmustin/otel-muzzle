// SPDX-License-Identifier: Apache-2.0

/**
 * The regex predicate: a subset of RE2 syntax, checked and translated for `RegExp`.
 *
 * The specification names RE2 syntax. `RegExp` is a backtracking engine with a different syntax, so a pattern is
 * read token by token against an allowlist, and rewritten where `RegExp` would give a construct another meaning:
 * `.` matches everything but `\n`, `\s` is `[\t\n\f\r ]`, and ASCII punctuation is written as `\xHH`. Whatever RE2
 * lacks (backreferences, lookaround) or lies outside the subset is an error. The `RegExp` never has the `u` flag,
 * under which `\w` and `\b` stop being ASCII when `i` is set, nor the `g` or `y` flag, under which it would keep a
 * position between calls. See docs/policy-conformance.md#regex.
 */

const FLAGS = /^\(\?([ims]+)\)/;
const REPEAT = /^\{([0-9]+)(?:(,)([0-9]*))?\}/;
const MAX_REPEAT = 1000;
const HEX_PAIR = /^[0-9a-fA-F]{2}$/;
const PUNCTUATION = new Set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~');
const CLASS_ESCAPES = new Set('dDwW');
const CONTROL = new Map([
  ['t', '\t'],
  ['n', '\n'],
  ['r', '\r'],
  ['f', '\f'],
  ['v', '\v'],
]);
const SPACE = '\\t\\n\\f\\r\\x20';
const DOUBLED = new Set('-&|~');

class Translator {
  private readonly _pattern: string;
  private _pos = 0;
  private _flags = '';
  private _dotAll = false;
  private readonly _out: string[] = [];

  constructor(pattern: string) {
    this._pattern = pattern;
  }

  public translate(): RegExp {
    const pattern = this._pattern;
    const leading = FLAGS.exec(pattern);
    if (leading !== null) {
      this._flags = [...'ims']
        .filter(flag => leading[1].includes(flag))
        .join('');
      this._dotAll = leading[1].includes('s');
      this._pos = leading[0].length;
    }
    let depth = 0;
    let atom = false; // whether the previous token can take a quantifier
    while (this._pos < pattern.length) {
      const char = pattern[this._pos];
      if (char === '\\') {
        atom = this._escape();
      } else if (char === '[') {
        this._charClass();
        atom = true;
      } else if (char === '(') {
        if (pattern.startsWith('(?:', this._pos)) {
          this._emit('(?:', 3);
        } else if (pattern.startsWith('(?', this._pos)) {
          throw this._fail('group syntax');
        } else {
          this._emit('(', 1);
        }
        depth += 1;
        atom = false;
      } else if (char === ')') {
        if (depth === 0) throw this._fail('unbalanced parenthesis');
        depth -= 1;
        this._emit(')', 1);
        atom = true;
      } else if (char === '|') {
        this._emit('|', 1);
        atom = false;
      } else if (char === '*' || char === '+' || char === '?' || char === '{') {
        if (!atom) throw this._fail('nothing to repeat');
        this._quantifier();
        atom = false;
      } else if (char === '}' || char === ']') {
        throw this._fail(`unescaped ${char}`);
      } else if (char === '^' || char === '$') {
        // Without the m flag, both match only at the ends of the text, as in RE2. With it, RegExp also takes
        // "\r", U+2028 and U+2029 for line ends, which RE2 does not (docs/policy-conformance.md#regex).
        this._emit(char, 1);
        atom = false;
      } else if (char === '.') {
        this._emit(this._dotAll ? '.' : '[^\\n]', 1);
        atom = true;
      } else {
        this._emit(literal(char), 1);
        atom = true;
      }
    }
    if (depth !== 0) throw this._fail('unbalanced parenthesis');
    try {
      return new RegExp(this._out.join(''), this._flags);
    } catch (error) {
      throw new Error(`unsupported regex: ${(error as Error).message}`);
    }
  }

  private _fail(reason: string, at: number = this._pos): Error {
    return new Error(`unsupported regex: ${reason} at ${at}`);
  }

  private _emit(text: string, width: number): void {
    this._out.push(text);
    this._pos += width;
  }

  private _quantifier(): void {
    const pattern = this._pattern;
    if (pattern[this._pos] === '{') {
      const repeat = REPEAT.exec(pattern.slice(this._pos));
      if (repeat === null) throw this._fail('repetition');
      const low = Number(repeat[1]);
      const high = repeat[3] ? Number(repeat[3]) : low;
      if (low > MAX_REPEAT || high > MAX_REPEAT || high < low) {
        throw this._fail('repetition count');
      }
      this._emit(repeat[0], repeat[0].length);
    } else {
      this._emit(pattern[this._pos], 1);
    }
    if (pattern.startsWith('?', this._pos)) this._emit('?', 1);
  }

  /** Reads an escape that stands for one character, and returns its translation. Undefined: not such an escape. */
  private _single(): string | undefined {
    const pattern = this._pattern;
    const char = pattern.charAt(this._pos + 1);
    if (CONTROL.has(char)) {
      this._pos += 2;
      return `\\${char}`;
    }
    if (char === 'x') {
      const digits = pattern.slice(this._pos + 2, this._pos + 4);
      if (!HEX_PAIR.test(digits) || parseInt(digits, 16) > 0x7f) {
        throw this._fail('\\x escape');
      }
      this._pos += 4;
      return `\\x${digits.toLowerCase()}`;
    }
    if (PUNCTUATION.has(char)) {
      this._pos += 2;
      return literal(char);
    }
    return undefined;
  }

  /** An escape outside a class. Returns whether it can take a quantifier. */
  private _escape(): boolean {
    const char = this._pattern.charAt(this._pos + 1);
    if (CLASS_ESCAPES.has(char)) {
      this._emit(`\\${char}`, 2);
      return true;
    }
    if (char === 's') {
      this._emit(`[${SPACE}]`, 2);
      return true;
    }
    if (char === 'S') {
      this._emit(`[^${SPACE}]`, 2);
      return true;
    }
    if (char === 'b' || char === 'B') {
      this._emit(`\\${char}`, 2);
      return false;
    }
    const single = this._single();
    if (single === undefined) throw this._fail('escape');
    this._out.push(single);
    return true;
  }

  private _charClass(): void {
    const pattern = this._pattern;
    const start = this._pos;
    this._pos += 1;
    const out = ['['];
    if (pattern.startsWith('^', this._pos)) {
      out.push('^');
      this._pos += 1;
    }
    if (pattern.startsWith(']', this._pos)) {
      throw this._fail('character class starting with ]');
    }
    for (;;) {
      if (this._pos >= pattern.length) {
        throw this._fail('unterminated character class', start);
      }
      if (pattern[this._pos] === ']') {
        this._pos += 1;
        break;
      }
      const low = this._classItem(out);
      if (low === undefined) continue;
      if (
        pattern.startsWith('-', this._pos) &&
        !pattern.startsWith(']', this._pos + 1)
      ) {
        if (this._pos + 1 >= pattern.length) {
          throw this._fail('unterminated character class', start);
        }
        this._pos += 1;
        const high = this._classItem(undefined);
        if (high === undefined || code(high) < code(low)) {
          throw this._fail('character class range');
        }
        out.push(`${low}-${high}`);
      } else {
        out.push(low);
      }
    }
    out.push(']');
    this._out.push(out.join(''));
  }

  /**
   * Reads one class member. Returns a single character's translation, or undefined after appending a set to `out`.
   *
   * @param out The class so far; undefined when the member is the end of a range, where a set is an error.
   */
  private _classItem(out: string[] | undefined): string | undefined {
    const pattern = this._pattern;
    const char = pattern[this._pos];
    if (char === '\\') {
      const escaped = pattern.charAt(this._pos + 1);
      if (CLASS_ESCAPES.has(escaped) || escaped === 's') {
        if (out === undefined) throw this._fail('character class range');
        out.push(escaped === 's' ? SPACE : `\\${escaped}`);
        this._pos += 2;
        if (
          pattern.startsWith('-', this._pos) &&
          !pattern.startsWith(']', this._pos + 1)
        ) {
          throw this._fail('character class range');
        }
        return undefined;
      }
      const single = this._single();
      if (single === undefined) throw this._fail('escape in character class');
      return single;
    }
    if (char === '[') throw this._fail('unescaped [ in character class');
    if (DOUBLED.has(char) && pattern.startsWith(char, this._pos + 1)) {
      throw this._fail(`${char}${char} in character class`);
    }
    this._pos += 1;
    return literal(char);
  }
}

/** A character that stands for itself, written so that no engine reads it as syntax. */
function literal(char: string): string {
  return PUNCTUATION.has(char)
    ? `\\x${char.charCodeAt(0).toString(16).padStart(2, '0')}`
    : char;
}

/** The character code of a class member that `literal` or an escape produced. */
function code(item: string): number {
  if (item.startsWith('\\x')) return parseInt(item.slice(2), 16);
  if (item.startsWith('\\')) return CONTROL.get(item[1])!.charCodeAt(0);
  return item.charCodeAt(0);
}

/**
 * Compiles an RE2-syntax pattern from the supported subset. Use `test` on the result: it matches anywhere in the
 * text. Throws an `Error` whose message starts with `unsupported regex:` when the pattern uses a construct RE2
 * lacks, or one outside the supported subset.
 *
 * @param pattern The pattern as the policy wrote it.
 */
export function compileRegex(pattern: string): RegExp {
  return new Translator(pattern).translate();
}
