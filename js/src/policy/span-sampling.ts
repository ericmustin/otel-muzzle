// SPDX-License-Identifier: Apache-2.0

/**
 * Span sampling policies, compiled and evaluated at span start. See docs/design.md#policy-mode.
 *
 * Every enabled, valid `SpanSamplingPolicy` whose matchers all match a span contributes a rejection threshold, and
 * the highest one (the lowest probability) decides. A span no policy matches is kept.
 */
import {
  isSpanContextValid,
  SpanKind,
  type Attributes,
  type SpanContext,
  type TraceState,
} from '@opentelemetry/api';
import { logger } from '../utils';
import {
  isInvalidPolicy,
  type InvalidPolicy,
  type Policy,
  type PolicyApplyStatus,
  type PolicyImplementer,
  type Predicate,
  type Scalar,
  type SpanMatcher,
  type SpanRecordField,
  type SpanSamplingPolicy,
} from './model';
import { compileRegex } from './regex';
import {
  parseOt,
  proportional,
  randomness,
  threshold,
  thresholdText,
} from './threshold';

/** What a sampler knows about a span when it starts. */
interface SpanAtStart {
  readonly traceId: string;
  readonly name: string;
  readonly kind: SpanKind | undefined;
  readonly parent: SpanContext | undefined;
  readonly traceState: TraceState | undefined;
  readonly attributes: Attributes | undefined;
}

type Matcher = (span: SpanAtStart) => boolean;

interface CompiledPolicy {
  readonly id: string;
  readonly matchers: readonly Matcher[];
  readonly ratio: number;
  readonly precision: number;
  readonly proportional: boolean;
  /** The policy's own threshold, as text. */
  readonly threshold: string;
  /** The last arriving threshold a proportional policy saw, and the threshold it gave: a trace repeats one. */
  lastArriving?: string;
  lastProportional?: string;
}

const ABSENT = Symbol('absent');
const KIND_NAMES = ['INTERNAL', 'SERVER', 'CLIENT', 'PRODUCER', 'CONSUMER'];

// A first-class field of a span, as the text a predicate reads. An empty text means the field is not set.
// SPAN_ID has no reader: a sampler decides before the span id exists.
const RECORD_FIELDS = new Map<SpanRecordField, (span: SpanAtStart) => string>([
  ['NAME', span => span.name],
  ['TRACE_ID', span => span.traceId],
  [
    'PARENT_SPAN_ID',
    ({ parent }) =>
      parent !== undefined && isSpanContextValid(parent) ? parent.spanId : '',
  ],
  ['TRACE_STATE', span => span.traceState?.serialize() ?? ''],
  // A span whose kind is unset reads INTERNAL, the default.
  ['KIND', span => KIND_NAMES[span.kind ?? SpanKind.INTERNAL] ?? ''],
]);

const COMPARE = {
  gt: (value: number | bigint, other: number | bigint) => value > other,
  gte: (value: number | bigint, other: number | bigint) => value >= other,
  lt: (value: number | bigint, other: number | bigint) => value < other,
  lte: (value: number | bigint, other: number | bigint) => value <= other,
};

function isNumber(value: unknown): value is number | bigint {
  return typeof value === 'number' || typeof value === 'bigint';
}

/** The same bytes as a Buffer, without copying them. */
function asBuffer(bytes: Uint8Array): Buffer {
  return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

/** Same type and value. An integer and a double are compared in one numeric domain, exactly. */
function equal(value: unknown, scalar: Scalar): boolean {
  if (typeof scalar === 'string' || typeof scalar === 'boolean') {
    return value === scalar;
  }
  if (isNumber(scalar)) {
    // eslint-disable-next-line eqeqeq -- == compares a number and a bigint by value, exactly; === is never true for the pair
    return isNumber(value) && value == scalar;
  }
  return value instanceof Uint8Array && Buffer.compare(value, scalar) === 0;
}

/** The predicate as a function of a value that is present. A value of the wrong type does not match. */
function test(
  predicate: Exclude<Predicate, { kind: 'exists' }>
): (value: unknown) => boolean {
  switch (predicate.kind) {
    case 'equals': {
      const scalar = predicate.value;
      return value => equal(value, scalar);
    }
    case 'regex': {
      const regex = compileRegex(predicate.pattern);
      return value => typeof value === 'string' && regex.test(value);
    }
    case 'compare': {
      const compare = COMPARE[predicate.op];
      const number = predicate.value;
      return value => isNumber(value) && compare(value, number);
    }
    case 'contains': {
      const wanted = predicate.value;
      return value => {
        if (Array.isArray(value)) {
          return value.some(element => equal(element, wanted));
        }
        if (typeof value === 'string') {
          return typeof wanted === 'string' && value.includes(wanted);
        }
        return (
          value instanceof Uint8Array &&
          wanted instanceof Uint8Array &&
          asBuffer(value).includes(asBuffer(wanted))
        );
      };
    }
  }
}

/** The attribute at `path`, each segment a key of an object, or `ABSENT`. Start attributes are flat. */
function attribute(span: SpanAtStart, path: readonly string[]): unknown {
  let current: unknown = span.attributes;
  for (const segment of path) {
    if (
      typeof current !== 'object' ||
      current === null ||
      Array.isArray(current) ||
      !Object.hasOwn(current, segment)
    ) {
      return ABSENT;
    }
    current = (current as Record<string, unknown>)[segment];
  }
  return current === undefined || current === null ? ABSENT : current;
}

/** Compiles one matcher. Throws when the policy uses something a sampler cannot evaluate. */
function compileMatcher(matcher: SpanMatcher, where: string): Matcher {
  const { target, predicate, negate } = matcher;
  if (target.kind === 'recordField') {
    const read = RECORD_FIELDS.get(target.field);
    if (read === undefined) {
      throw new Error(
        `${where}.target: unsupported selector SPAN_RECORD_FIELD_${target.field}`
      );
    }
    if (predicate.kind === 'exists')
      return span => (read(span) !== '') !== negate;
    const matches = test(predicate);
    return span => matches(read(span)) !== negate;
  }
  if (target.kind === 'spanAttribute') {
    const { path } = target;
    if (predicate.kind === 'exists') {
      return span => (attribute(span, path) !== ABSENT) !== negate;
    }
    const matches = test(predicate);
    return span => {
      const value = attribute(span, path);
      return (value !== ABSENT && matches(value)) !== negate;
    };
  }
  throw new Error(`${where}.target: unsupported selector ${target.selector}`);
}

function compile(id: string, policy: SpanSamplingPolicy): CompiledPolicy {
  const { percentage, precision, mode } = policy.sampling;
  const ratio = percentage / 100;
  return {
    id,
    matchers: policy.matches.map((matcher, index) =>
      compileMatcher(matcher, `matches[${index}]`)
    ),
    ratio,
    precision,
    proportional: mode === 'PROPORTIONAL',
    threshold: thresholdText(threshold(ratio, precision)),
  };
}

function matchesAll(policy: CompiledPolicy, span: SpanAtStart): boolean {
  for (const matcher of policy.matchers) {
    if (!matcher(span)) return false;
  }
  return true;
}

/** The threshold a matching policy asks for, given the consistent threshold the span arrived with, if any. */
function thresholdFor(
  policy: CompiledPolicy,
  arriving: string | undefined
): string {
  if (arriving === undefined) return policy.threshold;
  let target = policy.threshold;
  if (policy.proportional) {
    if (policy.lastArriving !== arriving) {
      policy.lastProportional = thresholdText(
        proportional(BigInt(`0x${arriving}`), policy.ratio, policy.precision)
      );
      policy.lastArriving = arriving;
    }
    target = policy.lastProportional!;
  }
  // A threshold is never lowered.
  return target > arriving ? target : arriving;
}

/** The span sampling policies in effect. `applyPolicies` replaces them in one assignment; `keep` decides. */
export class SpanSamplingImplementer implements PolicyImplementer {
  private _compiled: readonly CompiledPolicy[] = [];
  private _failed = new Set<string>();
  private readonly _warn: (message: string) => void;

  /**
   * @param warn Receives each warning. Defaults to the component logger.
   */
  constructor(
    warn: (message: string) => void = message => logger.warn(message)
  ) {
    this._warn = warn;
  }

  /** Whether any policy is in effect. */
  public get active(): boolean {
    return this._compiled.length > 0;
  }

  public applyPolicies(
    policies: ReadonlyArray<Policy | InvalidPolicy>
  ): PolicyApplyStatus[] {
    const statuses: PolicyApplyStatus[] = [];
    const compiled: CompiledPolicy[] = [];
    for (const policy of policies) {
      let error: string | undefined;
      if (isInvalidPolicy(policy)) {
        error = policy.error;
      } else if (!policy.enabled) {
        // Absent, by request.
      } else if (policy.spanSampling === undefined) {
        error = `unsupported policy type: ${policy.unsupported}`;
      } else {
        try {
          compiled.push(compile(policy.id, policy.spanSampling));
        } catch (cause) {
          error = (cause as Error).message;
        }
      }
      if (error === undefined) {
        statuses.push({ policyId: policy.id, applied: true });
      } else {
        this._warn(`policy '${policy.id}' is not applied: ${error}`);
        statuses.push({ policyId: policy.id, applied: false, error });
      }
    }
    this._failed = new Set();
    this._compiled = compiled;
    return statuses;
  }

  /**
   * Returns whether the policies keep a span.
   *
   * @param traceId The span's trace id.
   * @param name The span's name at start.
   * @param kind The span's kind; undefined reads INTERNAL.
   * @param parent The parent's span context; undefined or invalid for a root span.
   * @param traceState The span's tracestate: the sampler's, or else the parent's.
   * @param attributes The attributes passed at start.
   */
  public keep(
    traceId: string,
    name: string,
    kind: SpanKind | undefined,
    parent: SpanContext | undefined,
    traceState: TraceState | undefined,
    attributes: Attributes | undefined
  ): boolean {
    const compiled = this._compiled;
    if (compiled.length === 0) return true;
    const span = { traceId, name, kind, parent, traceState, attributes };
    let limit: string | undefined;
    let value = '';
    let arriving: string | undefined;
    for (const policy of compiled) {
      try {
        if (!matchesAll(policy, span)) continue;
      } catch (error) {
        // Fail open: a policy that cannot be evaluated matches nothing.
        this._report(policy.id, error);
        continue;
      }
      if (limit === undefined) {
        const [rv, th] = parseOt(traceState?.get('ot'));
        const random = randomness(traceId, rv);
        // Nothing to compare a threshold with: keep the span.
        if (random === undefined) return true;
        value = random;
        // A threshold above the randomness is inconsistent with it, and ignored.
        arriving = th !== undefined && value >= th ? th : undefined;
      }
      const target = thresholdFor(policy, arriving);
      if (limit === undefined || target > limit) limit = target;
    }
    return limit === undefined || value >= limit;
  }

  private _report(policyId: string, error: unknown): void {
    if (this._failed.has(policyId)) return;
    this._failed.add(policyId);
    this._warn(
      `policy '${policyId}' could not be evaluated and matches nothing until it can: ${String(error)}`
    );
  }
}
