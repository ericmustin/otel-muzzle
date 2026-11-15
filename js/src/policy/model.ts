// SPDX-License-Identifier: Apache-2.0

/**
 * The policy model: what a policy file says, after parsing.
 *
 * It mirrors the messages of `opentelemetry.proto.policies.v1development` that span sampling needs: `Policy`,
 * `SpanSamplingPolicy`, `SpanMatcher`, `SpanFieldSelector`, `Predicate` and `SamplingConfig`.
 */

/**
 * A `ScalarValue`. A number is an `int_value` or a `double_value`: the two compare in one numeric domain, so the
 * model does not tell them apart. An `int_value` outside the range a number holds exactly is a bigint.
 */
export type Scalar = string | number | bigint | boolean | Uint8Array;

/** `SpanRecordField`, without its `SPAN_RECORD_FIELD_` prefix. */
export type SpanRecordField =
  | 'NAME'
  | 'TRACE_ID'
  | 'SPAN_ID'
  | 'PARENT_SPAN_ID'
  | 'TRACE_STATE'
  | 'KIND';

/** `SamplingMode`, without its `SAMPLING_MODE_` prefix. An unspecified mode is EQUALIZING. */
export type SamplingMode = 'EQUALIZING' | 'PROPORTIONAL';

export interface RecordField {
  readonly kind: 'recordField';
  readonly field: SpanRecordField;
}

export interface SpanAttribute {
  readonly kind: 'spanAttribute';
  readonly path: readonly string[];
}

/** A selector the specification defines and a sampler cannot read: resource and scope targets. */
export interface UnsupportedTarget {
  readonly kind: 'unsupported';
  /** The selector's proto name: `resource_attribute`, `scope_attribute` or `scope_field`. */
  readonly selector: string;
}

export type Target = RecordField | SpanAttribute | UnsupportedTarget;

export interface Exists {
  readonly kind: 'exists';
}

export interface Equals {
  readonly kind: 'equals';
  readonly value: Scalar;
}

export interface Regex {
  readonly kind: 'regex';
  readonly pattern: string;
}

/** `gt`, `gte`, `lt` or `lte` against a `NumericValue`. */
export interface Compare {
  readonly kind: 'compare';
  readonly op: 'gt' | 'gte' | 'lt' | 'lte';
  readonly value: number | bigint;
}

export interface Contains {
  readonly kind: 'contains';
  readonly value: Scalar;
}

export type Predicate = Exists | Equals | Regex | Compare | Contains;

export interface SpanMatcher {
  readonly target: Target;
  readonly predicate: Predicate;
  readonly negate: boolean;
}

export interface SamplingConfig {
  /** The share of matching spans to keep, 0 to 100. */
  readonly percentage: number;
  readonly mode: SamplingMode;
  /** `sampling_precision`: 1 to 14 hexadecimal digits. */
  readonly precision: number;
}

export interface SpanSamplingPolicy {
  readonly matches: readonly SpanMatcher[];
  readonly sampling: SamplingConfig;
}

/**
 * A policy that parsed. `spanSampling` is undefined for a disabled policy, whose definition is not read, and for a
 * policy of another type, which `unsupported` then names (`log_filter`, `log_transform` or `metric_filter`).
 */
export interface Policy {
  readonly id: string;
  readonly enabled: boolean;
  readonly spanSampling?: SpanSamplingPolicy;
  readonly unsupported?: string;
}

/** A policy that did not parse. It stays inert. `id` is empty when the policy has no usable one. */
export interface InvalidPolicy {
  readonly id: string;
  readonly error: string;
}

/** Whether a policy is in effect, and if not, why. */
export interface PolicyApplyStatus {
  readonly policyId: string;
  readonly applied: boolean;
  readonly error?: string;
}

/** Puts a policy set into effect in the running SDK. A policy store hands each new set to one. */
export interface PolicyImplementer {
  /** Replaces the set in effect with `policies`, and returns one status per policy, in order. */
  applyPolicies(
    policies: ReadonlyArray<Policy | InvalidPolicy>
  ): PolicyApplyStatus[];
}

/** Tells an `InvalidPolicy` from a `Policy`. */
export function isInvalidPolicy(
  entry: Policy | InvalidPolicy
): entry is InvalidPolicy {
  return 'error' in entry;
}
