// SPDX-License-Identifier: Apache-2.0

/**
 * Rejection thresholds, the `ot` tracestate member, and randomness. See docs/design.md#policy-mode.
 *
 * A span is kept when its 56-bit randomness R is at or above the rejection threshold T, as in the OpenTelemetry
 * tracestate probability sampling specification. A threshold is computed as a BigInt when a policy is compiled.
 * Per span, R and T are compared as text: 14 lowercase hexadecimal digits order the way their values do.
 */

/** The threshold that keeps nothing: every randomness value is below it. */
export const MAX_THRESHOLD = 1n << 56n;
/** `MAX_THRESHOLD` as text. It is not hexadecimal: it only has to sort after every 14-digit value. */
export const KEEP_NOTHING = 'g';

const TWO_POW_56 = 2 ** 56;
const MAX_DIGITS = 14;
const MAX_OT_LENGTH = 256;
const TH = /^[0-9a-f]{1,14}$/;
const RV = /^[0-9a-f]{14}$/;
const TRACE_ID = /^[0-9a-f]{32}$/;
const FLOAT64 = new DataView(new ArrayBuffer(8));

/**
 * Returns the rejection threshold for a sampling probability in [0, 1]: `2**56 - round(probability * 2**56)`,
 * rounded half up to `precision` hexadecimal digits counted after the leading `f` digits, and to 14 digits at most.
 *
 * @param probability The share of spans to keep.
 * @param precision The number of hexadecimal digits to keep after the leading `f` digits, 1 to 14.
 */
export function threshold(probability: number, precision: number): bigint {
  if (probability <= 0) return MAX_THRESHOLD;
  if (probability >= 1) return 0n;
  // Multiplying by a power of two is exact, and so is everything after it.
  const scaled = probability * TWO_POW_56;
  const whole = Math.floor(scaled);
  let value =
    MAX_THRESHOLD - (BigInt(whole) + (scaled - whole >= 0.5 ? 1n : 0n));
  const leading = Math.floor(-binaryExponent(probability) / 4);
  const shift = BigInt(
    4 * (MAX_DIGITS - Math.min(MAX_DIGITS, precision + leading))
  );
  if (shift > 0n) value = ((value + (1n << (shift - 1n))) >> shift) << shift;
  return value;
}

/**
 * Returns the threshold for the arriving probability multiplied by `ratio` (SAMPLING_MODE_PROPORTIONAL).
 *
 * @param arriving The threshold the span arrived with.
 * @param ratio The policy's percentage as a probability.
 * @param precision The policy's sampling precision.
 */
export function proportional(
  arriving: bigint,
  ratio: number,
  precision: number
): bigint {
  return threshold(
    (Number(MAX_THRESHOLD - arriving) / TWO_POW_56) * ratio,
    precision
  );
}

/** Returns a threshold as the text a span's randomness is compared with: 14 hexadecimal digits, or `KEEP_NOTHING`. */
export function thresholdText(value: bigint): string {
  return value >= MAX_THRESHOLD
    ? KEEP_NOTHING
    : value.toString(16).padStart(MAX_DIGITS, '0');
}

/**
 * Returns `[rv, th]` from the value of the `ot` tracestate member, each as 14 hexadecimal digits (`th` is padded
 * with zeros on the right). An invalid sub-key reads as absent.
 *
 * @param ot The member's value, or undefined when the tracestate has no `ot` member.
 */
export function parseOt(
  ot: string | undefined
): [rv: string | undefined, th: string | undefined] {
  let rv: string | undefined;
  let th: string | undefined;
  if (ot === undefined || ot.length > MAX_OT_LENGTH) return [rv, th];
  for (const member of ot.split(';')) {
    if (member.startsWith('th:')) {
      const value = member.slice(3);
      th = TH.test(value) ? value.padEnd(MAX_DIGITS, '0') : undefined;
    } else if (member.startsWith('rv:')) {
      const value = member.slice(3);
      rv = RV.test(value) ? value : undefined;
    }
  }
  return [rv, th];
}

/**
 * Returns the explicit randomness value when there is one, else trace-id bytes 9-15: the last 14 hexadecimal
 * characters. Returns undefined when there is no `rv` and the trace id is not 32 lowercase hexadecimal characters
 * (the SDK never passes such an id).
 */
export function randomness(
  traceId: string,
  rv: string | undefined
): string | undefined {
  if (rv !== undefined) return rv;
  return TRACE_ID.test(traceId) ? traceId.slice(18) : undefined;
}

/** Returns `ot` without its `th` sub-key, or undefined when nothing else is left. */
export function eraseTh(ot: string): string | undefined {
  const erased = ot
    .split(';')
    .filter(member => !member.startsWith('th:'))
    .join(';');
  return erased === '' ? undefined : erased;
}

/**
 * Returns e for a positive double `m * 2**e` with `0.5 <= m < 1`, as C's `frexp` does. It reads the exponent bits,
 * because `Math.log2` rounds. A subnormal number reads -1022, above its real exponent: both are far below -56,
 * where the digit count has already reached 14.
 */
function binaryExponent(value: number): number {
  FLOAT64.setFloat64(0, value);
  return ((FLOAT64.getUint16(0) >> 4) & 0x7ff) - 1022;
}
