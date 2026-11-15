// SPDX-License-Identifier: Apache-2.0

import {
  isSpanContextValid,
  isValidTraceId,
  SpanKind,
  trace,
  type Attributes,
  type Context,
  type Link,
  type SpanContext,
  type TraceState,
} from '@opentelemetry/api';
import type { Sampler, SamplingResult } from '@opentelemetry/sdk-trace';
import { MUZZLE_SAMPLER } from './internal-types';
import {
  AlwaysOnSampler,
  ParentBasedSampler,
  SamplingDecision,
} from './load-sdk-trace';
import {
  fromFile,
  policyFileFromEnv,
  type PolicyRuntime,
} from './policy/runtime';
import type { SpanSamplingImplementer } from './policy/span-sampling';
import { eraseTh } from './policy/threshold';
import { samplerFromEnv } from './sampler-from-env';
import type { MuzzleSamplerOptions } from './types';
import { logger } from './utils';

const RATIO_ENV = 'OTEL_MUZZLE_VERBOSE_RATIO';
const DEFAULT_RATIO = 0.1;
/** The W3C tracestate member that forces a verbose trace: `muzzle=verbose`. */
const TRACESTATE_KEY = 'muzzle';
const VERBOSE = 'verbose';
/** What `OTEL_MUZZLE_VERBOSE_RATIO` accepts: a plain decimal number in ASCII digits. */
const RATIO_GRAMMAR = /^[+-]?([0-9]+(\.[0-9]+)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;
/** Surrounding ASCII whitespace; `trim()` would also strip Unicode spaces. */
const SURROUNDING_ASCII_WHITESPACE = /^[ \t\n\v\f\r]+|[ \t\n\v\f\r]+$/g;

/**
 * Drops INTERNAL spans whose parent is a local span, unless the trace is verbose. A trace is verbose when its
 * tracestate carries `muzzle=verbose` or when the ratio selects its trace id. Every other decision is the
 * delegate's, and the delegate's DROP always wins.
 *
 * The sampler only decides. Without `installMuzzle()`, the children of a dropped span point at a parent that is
 * never exported.
 *
 * In policy mode the rule above is replaced by span sampling policies: a span the delegate records is dropped when
 * the policies that match it say so, and kept otherwise. Policy mode is on when `policies` or `policyFile` is
 * given, or when `OTEL_MUZZLE_POLICY_FILE` names a file. The ratio is then ignored.
 */
export class MuzzleSampler implements Sampler {
  public readonly delegate: Sampler;
  /** The verbose ratio of the built-in rule. Undefined in policy mode. */
  public readonly ratio: number | undefined;
  /** The span sampling policies that decide in policy mode. Undefined otherwise. */
  public readonly policies: SpanSamplingImplementer | undefined;
  private readonly _ratioThreshold: number;
  /** Configuration warnings, logged on the first sampling decision, when `diag` is set up. */
  private _pendingWarnings: string[] | undefined = [];
  /** The policy file's runtime, which holds its own warnings for the first sampling decision. */
  private readonly _policyRuntime: PolicyRuntime | undefined;

  constructor(options: MuzzleSamplerOptions = {}) {
    const { delegate, ratio, policies, policyFile } = options;
    if (ratio !== undefined && !isRatio(ratio)) {
      const message = `otel-muzzle: ratio must be a number within [0, 1], got ${
        typeof ratio === 'string' ? JSON.stringify(ratio) : String(ratio)
      }`;
      throw typeof ratio === 'number'
        ? new RangeError(message)
        : new TypeError(message);
    }
    if (policyFile != null && typeof policyFile !== 'string') {
      throw new TypeError(
        `otel-muzzle: policyFile must be a string, got ${String(policyFile)}`
      );
    }
    const warnings = this._pendingWarnings!;
    const warn = (message: string) => void warnings.push(message);
    this.delegate =
      delegate ??
      samplerFromEnv(process.env, warn) ??
      new ParentBasedSampler({ root: new AlwaysOnSampler() });
    if (policies == null) {
      const path = policyFile || policyFileFromEnv();
      if (path !== undefined) this._policyRuntime = fromFile(path);
    }
    this.policies = policies ?? this._policyRuntime?.policies;
    if (this.policies === undefined) {
      this.ratio = ratio ?? ratioFromEnv(process.env, warn);
    } else if (ratio !== undefined) {
      warn(`ratio ${ratio} is ignored in policy mode`);
    }
    this._ratioThreshold = Math.floor((this.ratio ?? 0) * 2 ** 32);
  }

  public shouldSample(
    context: Context,
    traceId: string,
    spanName: string,
    spanKind: SpanKind,
    attributes: Attributes,
    links: Link[]
  ): SamplingResult {
    this._flushWarnings();
    const result = this.delegate.shouldSample(
      context,
      traceId,
      spanName,
      spanKind,
      attributes,
      links
    );
    if (result.decision === SamplingDecision.NOT_RECORD) return result;
    const parent = trace.getSpanContext(context);
    const policies = this.policies;
    if (policies !== undefined) {
      // The tracestate the span would get: the delegate's, or else the parent's.
      const traceState = result.traceState ?? parent?.traceState;
      return policies.keep(
        traceId,
        spanName,
        spanKind,
        parent,
        traceState,
        attributes
      )
        ? result
        : // A dropped span carries no sampling threshold, so that nothing downstream samples by one.
          {
            decision: SamplingDecision.NOT_RECORD,
            traceState: withoutTh(traceState),
          };
    }
    if (this._keep(parent, traceId, spanKind)) return result;
    return {
      decision: SamplingDecision.NOT_RECORD,
      traceState: result.traceState ?? parent?.traceState,
    };
  }

  public toString(): string {
    const mode =
      this.policies !== undefined ? 'mode=policy' : `ratio=${this.ratio}`;
    return `MuzzleSampler{${mode}, delegate=${this.delegate.toString()}}`;
  }

  public get [MUZZLE_SAMPLER](): boolean {
    return true;
  }

  /** Logs the queued configuration warnings once. */
  private _flushWarnings(): void {
    const warnings = this._pendingWarnings;
    if (warnings === undefined) return;
    this._pendingWarnings = undefined;
    for (const message of warnings) logger.warn(message);
    this._policyRuntime?.flushWarnings();
  }

  /**
   * Returns whether the span is kept. INTERNAL spans under a valid local parent are dropped, unless the parent's
   * tracestate carries `muzzle=verbose` or the ratio selects the trace.
   */
  private _keep(
    parent: SpanContext | undefined,
    traceId: string,
    kind: SpanKind | undefined
  ): boolean {
    if ((kind ?? SpanKind.INTERNAL) !== SpanKind.INTERNAL) return true;
    if (
      parent === undefined ||
      !isSpanContextValid(parent) ||
      parent.isRemote === true
    ) {
      return true;
    }
    if (parent.traceState?.get(TRACESTATE_KEY) === VERBOSE) return true;
    return this._isVerboseByRatio(traceId);
  }

  /**
   * Returns whether the ratio selects the trace. It reads trace-id bytes 12-15 (the last 8 hex characters) as a
   * big-endian uint32. They are random in OpenTelemetry, X-Ray, Datadog 128-bit and zero-padded 64-bit B3/Jaeger
   * ids. An invalid id selects nothing (the SDK never passes one).
   *
   * @param traceId The trace id of the span being sampled.
   */
  private _isVerboseByRatio(traceId: string): boolean {
    return (
      isValidTraceId(traceId) &&
      parseInt(traceId.slice(24, 32), 16) < this._ratioThreshold
    );
  }
}

/** Returns true for a MuzzleSampler, including one from another copy of this package. */
export function isMuzzleSampler(sampler: unknown): boolean {
  return (
    typeof sampler === 'object' &&
    sampler !== null &&
    (sampler as Record<symbol, unknown>)[MUZZLE_SAMPLER] === true
  );
}

/** Returns `traceState` without the `th` sub-key of its `ot` member; `traceState` itself when it has none. */
function withoutTh(traceState: TraceState | undefined): TraceState | undefined {
  const ot = traceState?.get('ot');
  if (traceState === undefined || ot === undefined) return traceState;
  const erased = eraseTh(ot);
  if (erased === ot) return traceState;
  return erased === undefined
    ? traceState.unset('ot')
    : traceState.set('ot', erased);
}

/** Returns `OTEL_MUZZLE_VERBOSE_RATIO`, or 0.1 when it is unset or blank. An invalid value is reported to `warn` and ignored. */
function ratioFromEnv(
  env: NodeJS.ProcessEnv,
  warn: (message: string) => void
): number {
  const raw = (env[RATIO_ENV] ?? '').replace(SURROUNDING_ASCII_WHITESPACE, '');
  if (raw === '') return DEFAULT_RATIO;
  const ratio = RATIO_GRAMMAR.test(raw) ? Number(raw) : Number.NaN;
  if (isRatio(ratio)) return ratio;
  warn(
    `${RATIO_ENV} must be a number within [0, 1], got ${JSON.stringify(raw)}; using ${DEFAULT_RATIO}`
  );
  return DEFAULT_RATIO;
}

function isRatio(value: unknown): value is number {
  return typeof value === 'number' && value >= 0 && value <= 1;
}
