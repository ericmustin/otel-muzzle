// SPDX-License-Identifier: Apache-2.0

/**
 * Reads a `PolicySet` from its Protobuf JSON form.
 *
 * Keys may be written in lowerCamelCase or in the proto's snake_case, enum values are names, and 64-bit integers
 * and floating point numbers may be strings, as Protobuf JSON allows. A policy that is not valid is returned as an
 * `InvalidPolicy` and never stops the others from parsing. Inside a span sampling policy an unknown key is an
 * error, because ignoring one could widen what the policy matches.
 */
import type {
  Compare,
  InvalidPolicy,
  Policy,
  Predicate,
  SamplingConfig,
  SamplingMode,
  Scalar,
  SpanMatcher,
  SpanRecordField,
  SpanSamplingPolicy,
  Target,
} from './model';
import { isInvalidPolicy } from './model';
import { compileRegex } from './regex';

type JsonObject = Record<string, unknown>;
/** A key as [lowerCamelCase, snake_case]. */
type Names = readonly [camel: string, snake: string];

const MISSING = Symbol('missing');
const INT64_MIN = -(2n ** 63n);
const INT64_MAX = 2n ** 63n - 1n;
const INTEGER = /^-?(?:0|[1-9][0-9]*)$/;
const NUMBER = /^-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/;
const NON_FINITE = new Map([
  ['NaN', Number.NaN],
  ['Infinity', Infinity],
  ['-Infinity', -Infinity],
]);
const BASE64 =
  /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const RECORD_FIELD_PREFIX = 'SPAN_RECORD_FIELD_';
const RECORD_FIELDS: ReadonlySet<string> = new Set<SpanRecordField>([
  'NAME',
  'TRACE_ID',
  'SPAN_ID',
  'PARENT_SPAN_ID',
  'TRACE_STATE',
  'KIND',
]);
const MODES = new Map<string, SamplingMode>([
  ['SAMPLING_MODE_UNSPECIFIED', 'EQUALIZING'],
  ['SAMPLING_MODE_EQUALIZING', 'EQUALIZING'],
  ['SAMPLING_MODE_PROPORTIONAL', 'PROPORTIONAL'],
]);
const same = (name: string): Names => [name, name];
const SPAN_SAMPLING: Names = ['spanSampling', 'span_sampling'];
const OTHER_DEFINITIONS: readonly Names[] = [
  ['logFilter', 'log_filter'],
  ['logTransform', 'log_transform'],
  ['metricFilter', 'metric_filter'],
];
const MATCHES = same('matches');
const SAMPLING = same('sampling');
const PERCENTAGE = same('percentage');
const MODE = same('mode');
const PRECISION: Names = ['samplingPrecision', 'sampling_precision'];
const TARGET = same('target');
const PREDICATE = same('predicate');
const NEGATE = same('negate');
const PATH = same('path');
const TARGETS: readonly Names[] = [
  ['recordField', 'record_field'],
  ['spanAttribute', 'span_attribute'],
  ['resourceAttribute', 'resource_attribute'],
  ['scopeAttribute', 'scope_attribute'],
  ['scopeField', 'scope_field'],
];
const SCALARS: readonly Names[] = [
  ['stringValue', 'string_value'],
  ['intValue', 'int_value'],
  ['boolValue', 'bool_value'],
  ['doubleValue', 'double_value'],
  ['bytesValue', 'bytes_value'],
];
const NUMERICS: readonly Names[] = [
  ['intValue', 'int_value'],
  ['doubleValue', 'double_value'],
];
const PREDICATES: readonly Names[] = [
  'exists',
  'equals',
  'regex',
  'gt',
  'gte',
  'lt',
  'lte',
  'contains',
].map(same);

/** The text holds no policy set at all: it is not JSON, or not the JSON of a `PolicySet`. */
export class PolicyDocumentError extends Error {
  public override readonly name = 'PolicyDocumentError';
}

/** One policy is invalid. The message names where. */
class Invalid extends Error {}

function fail(where: string, reason: string): never {
  throw new Invalid(`${where}: ${reason}`);
}

/** A value as an error message shows it: a string in single quotes, anything else as JSON. */
function show(value: unknown): string {
  return typeof value === 'string' ? `'${value}'` : JSON.stringify(value);
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Parses a policy file. The result has one entry per policy, in document order. Throws a `PolicyDocumentError`
 * when the text is not the JSON of a policy set.
 *
 * @param text The file's content.
 */
export function parsePolicySet(text: string): Array<Policy | InvalidPolicy> {
  let document: unknown;
  try {
    // JSON.parse takes no NaN or Infinity literal, as Protobuf JSON asks.
    document = JSON.parse(text);
  } catch (error) {
    throw new PolicyDocumentError(
      `not valid JSON: ${(error as Error).message}`
    );
  }
  if (!isObject(document)) {
    throw new PolicyDocumentError('a policy set must be a JSON object');
  }
  const policies = Object.hasOwn(document, 'policies')
    ? document.policies
    : undefined;
  if (policies === undefined || policies === null) return [];
  if (!Array.isArray(policies)) {
    throw new PolicyDocumentError('policies must be a list');
  }
  const entries = policies.map(policy);
  // An id names one policy. A repeated id makes every copy invalid: keeping the first or the last would make the
  // outcome depend on the order of the set, which the specification rules out. A disabled policy is absent.
  const present = (entry: Policy | InvalidPolicy) =>
    entry.id !== '' && (isInvalidPolicy(entry) || entry.enabled);
  const counts = new Map<string, number>();
  for (const entry of entries) {
    if (present(entry)) counts.set(entry.id, (counts.get(entry.id) ?? 0) + 1);
  }
  return entries.map((entry, index) =>
    present(entry) && counts.get(entry.id)! > 1
      ? {
          id: entry.id,
          error: `policies[${index}].id: duplicate id ${show(entry.id)}`,
        }
      : entry
  );
}

function policy(raw: unknown, index: number): Policy | InvalidPolicy {
  const where = `policies[${index}]`;
  const rawId = isObject(raw) && Object.hasOwn(raw, 'id') ? raw.id : undefined;
  const id = typeof rawId === 'string' ? rawId : '';
  try {
    if (!isObject(raw)) fail(where, 'a policy must be an object');
    if (id === '') fail(`${where}.id`, 'a policy needs a non-empty string id');
    const enabled = get(raw, same('enabled'), where);
    if (enabled !== MISSING && typeof enabled !== 'boolean') {
      fail(`${where}.enabled`, 'must be a boolean');
    }
    if (enabled === false) return { id, enabled: false };
    return definition(raw, id, where);
  } catch (error) {
    if (error instanceof Invalid) return { id, error: error.message };
    throw error;
  }
}

function definition(raw: JsonObject, id: string, where: string): Policy {
  const spanSampling = get(raw, SPAN_SAMPLING, where);
  const others = OTHER_DEFINITIONS.filter(
    names => get(raw, names, where) !== MISSING
  ).map(([, snake]) => snake);
  const count = others.length + (spanSampling === MISSING ? 0 : 1);
  if (count === 0) fail(where, 'a policy needs a definition');
  if (count > 1) fail(where, 'a policy has exactly one definition');
  if (others.length > 0) return { id, enabled: true, unsupported: others[0] };
  return {
    id,
    enabled: true,
    spanSampling: parseSpanSampling(spanSampling, `${where}.spanSampling`),
  };
}

/** The value under either spelling of a key, or `MISSING`. A null is missing, as in Protobuf JSON. */
function get(obj: JsonObject, [camel, snake]: Names, where: string): unknown {
  const hasCamel = Object.hasOwn(obj, camel);
  const hasSnake = Object.hasOwn(obj, snake);
  if (camel !== snake && hasCamel && hasSnake) {
    fail(where, `both ${camel} and ${snake} are set`);
  }
  const value = hasCamel ? obj[camel] : hasSnake ? obj[snake] : undefined;
  return value === undefined || value === null ? MISSING : value;
}

/** Checks that `value` is an object with no key outside `known`. */
function object(value: unknown, where: string, ...known: Names[]): JsonObject {
  if (!isObject(value)) fail(where, 'must be an object');
  const allowed = new Set(known.flat());
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(where, `unknown key ${show(key)}`);
  }
  return value;
}

/** The one variant that is set, as [snake_case name, value]. */
function oneOf(
  obj: JsonObject,
  choices: readonly Names[],
  where: string
): [name: string, value: unknown] {
  const found: Array<[string, unknown]> = [];
  for (const names of choices) {
    const value = get(obj, names, where);
    if (value !== MISSING) found.push([names[1], value]);
  }
  if (found.length !== 1) {
    const all = choices.map(([camel]) => camel).join(', ');
    fail(where, `exactly one of ${all} must be set`);
  }
  return found[0];
}

function parseSpanSampling(raw: unknown, where: string): SpanSamplingPolicy {
  const obj = object(raw, where, MATCHES, SAMPLING);
  let matches = get(obj, MATCHES, where);
  if (matches === MISSING) matches = [];
  if (!Array.isArray(matches)) fail(`${where}.matches`, 'must be a list');
  const matchers = matches.map((item, index) =>
    matcher(item, `${where}.matches[${index}]`)
  );
  const sampling = get(obj, SAMPLING, where);
  if (sampling === MISSING) fail(`${where}.sampling`, 'must be set');
  return {
    matches: matchers,
    sampling: parseSampling(sampling, `${where}.sampling`),
  };
}

function parseSampling(raw: unknown, where: string): SamplingConfig {
  const obj = object(raw, where, PERCENTAGE, MODE, PRECISION);
  const rawPercentage = get(obj, PERCENTAGE, where);
  if (rawPercentage === MISSING) fail(`${where}.percentage`, 'must be set');
  const percentage = double(rawPercentage, `${where}.percentage`);
  // NaN fails this too.
  if (!(percentage >= 0 && percentage <= 100)) {
    fail(`${where}.percentage`, 'must be within 0 to 100');
  }
  let mode = get(obj, MODE, where);
  if (mode === MISSING) mode = 'SAMPLING_MODE_UNSPECIFIED';
  const samplingMode = typeof mode === 'string' ? MODES.get(mode) : undefined;
  if (samplingMode === undefined) {
    fail(`${where}.mode`, `unknown sampling mode ${show(mode)}`);
  }
  const rawPrecision = get(obj, PRECISION, where);
  const precision =
    rawPrecision === MISSING
      ? 4
      : integer(rawPrecision, `${where}.samplingPrecision`);
  if (!(precision >= 1 && precision <= 14)) {
    fail(`${where}.samplingPrecision`, 'must be within 1 to 14');
  }
  return { percentage, mode: samplingMode, precision: Number(precision) };
}

function matcher(raw: unknown, where: string): SpanMatcher {
  const obj = object(raw, where, TARGET, PREDICATE, NEGATE);
  const rawTarget = get(obj, TARGET, where);
  if (rawTarget === MISSING) fail(`${where}.target`, 'must be set');
  const rawPredicate = get(obj, PREDICATE, where);
  if (rawPredicate === MISSING) fail(`${where}.predicate`, 'must be set');
  const negate = get(obj, NEGATE, where);
  if (negate !== MISSING && typeof negate !== 'boolean') {
    fail(`${where}.negate`, 'must be a boolean');
  }
  return {
    target: target(rawTarget, `${where}.target`),
    predicate: predicate(rawPredicate, `${where}.predicate`),
    negate: negate === true,
  };
}

function target(raw: unknown, where: string): Target {
  const [kind, value] = oneOf(object(raw, where, ...TARGETS), TARGETS, where);
  if (kind === 'record_field') {
    const field =
      typeof value === 'string' && value.startsWith(RECORD_FIELD_PREFIX)
        ? value.slice(RECORD_FIELD_PREFIX.length)
        : '';
    if (!RECORD_FIELDS.has(field)) {
      fail(`${where}.recordField`, `unknown span record field ${show(value)}`);
    }
    return { kind: 'recordField', field: field as SpanRecordField };
  }
  if (kind === 'scope_field') {
    if (typeof value !== 'string') {
      fail(`${where}.scopeField`, 'must be a scope field name');
    }
    return { kind: 'unsupported', selector: kind };
  }
  const path = get(object(value, `${where}.${kind}`, PATH), PATH, where);
  if (
    !Array.isArray(path) ||
    path.length === 0 ||
    !path.every(segment => typeof segment === 'string')
  ) {
    fail(`${where}.${kind}.path`, 'must be a non-empty list of strings');
  }
  return kind === 'span_attribute'
    ? { kind: 'spanAttribute', path: path as string[] }
    : { kind: 'unsupported', selector: kind };
}

function predicate(raw: unknown, where: string): Predicate {
  const [kind, value] = oneOf(
    object(raw, where, ...PREDICATES),
    PREDICATES,
    where
  );
  const at = `${where}.${kind}`;
  if (kind === 'exists') {
    object(value, at);
    return { kind: 'exists' };
  }
  if (kind === 'equals') return { kind: 'equals', value: scalar(value, at) };
  if (kind === 'contains') {
    return { kind: 'contains', value: scalar(value, at) };
  }
  if (kind === 'regex') {
    if (typeof value !== 'string') fail(at, 'must be a string');
    try {
      compileRegex(value);
    } catch (error) {
      fail(at, (error as Error).message);
    }
    return { kind: 'regex', pattern: value };
  }
  const [name, number] = oneOf(object(value, at, ...NUMERICS), NUMERICS, at);
  return {
    kind: 'compare',
    op: kind as Compare['op'],
    value:
      name === 'int_value'
        ? integer(number, `${at}.intValue`)
        : double(number, `${at}.doubleValue`),
  };
}

function scalar(raw: unknown, where: string): Scalar {
  const [name, value] = oneOf(object(raw, where, ...SCALARS), SCALARS, where);
  if (name === 'string_value') {
    if (typeof value !== 'string') {
      fail(`${where}.stringValue`, 'must be a string');
    }
    return value;
  }
  if (name === 'bool_value') {
    if (typeof value !== 'boolean') {
      fail(`${where}.boolValue`, 'must be a boolean');
    }
    return value;
  }
  if (name === 'int_value') return integer(value, `${where}.intValue`);
  if (name === 'double_value') return double(value, `${where}.doubleValue`);
  if (typeof value !== 'string' || !BASE64.test(value)) {
    fail(`${where}.bytesValue`, 'must be base64');
  }
  return Buffer.from(value, 'base64');
}

/**
 * A 64-bit integer: a whole JSON number that every JSON reader holds exactly, or decimal text. The result is a
 * number when a number holds it exactly, and a bigint otherwise.
 */
function integer(value: unknown, where: string): number | bigint {
  if (typeof value === 'string') {
    if (INTEGER.test(value)) {
      const number = BigInt(value);
      if (number >= INT64_MIN && number <= INT64_MAX) {
        return Number.isSafeInteger(Number(number)) ? Number(number) : number;
      }
    }
  } else if (typeof value === 'number' && Number.isSafeInteger(value)) {
    return value;
  }
  return fail(where, 'must be a 64-bit integer');
}

function double(value: unknown, where: string): number {
  if (typeof value === 'string') {
    const nonFinite = NON_FINITE.get(value);
    if (nonFinite !== undefined) return nonFinite;
    if (NUMBER.test(value)) return Number(value);
  } else if (typeof value === 'number') {
    return value;
  }
  return fail(where, 'must be a number');
}
