// SPDX-License-Identifier: Apache-2.0

import { trace, type Tracer, type TracerProvider } from '@opentelemetry/api';
import { isMuzzleSampler } from './muzzle-sampler';
import { patchTracerPrototype } from './patch';
import type { InstallMuzzleOptions } from './types';
import { logger } from './utils';

/**
 * Applies the reparenting patch to the SDK `Tracer` behind the tracer provider, once. It reaches tracers created
 * before the call, and the API's proxy tracers. Call it after `NodeSDK.start()` (or after registering your
 * `TracerProvider`), with `sampler: new MuzzleSampler()` in that SDK's configuration.
 *
 * Returns true when the SDK's tracer is patched (now or before). Returns false, and logs a warning, when no SDK
 * provider is registered; under `OTEL_SDK_DISABLED=true` it returns false quietly.
 *
 * @param options The tracer provider to patch; defaults to the registered one.
 */
export function installMuzzle(options: InstallMuzzleOptions = {}): boolean {
  const tracer = findSdkTracer(options.tracerProvider);
  if (tracer === undefined) {
    if (isSdkDisabled()) {
      logger.debug('OTEL_SDK_DISABLED=true, so installMuzzle() did nothing');
    } else {
      logger.warn(
        'no OpenTelemetry SDK TracerProvider is registered, so installMuzzle() did nothing. Call it after ' +
          'NodeSDK.start(); NodeSDK registers a provider only when it has a span processor or a trace exporter.'
      );
    }
    return false;
  }
  patchTracerPrototype(Object.getPrototypeOf(tracer));
  // `_sampler` is a private field of the SDK Tracer, read only for this warning: an SDK without it gets none.
  const sampler = (tracer as { _sampler?: unknown })._sampler;
  if (sampler !== undefined && !isMuzzleSampler(sampler)) {
    logger.warn(
      `the tracer provider samples with ${String(sampler)}, not a MuzzleSampler, so no spans are dropped. ` +
        'Pass sampler: new MuzzleSampler() to NodeSDK or to the TracerProvider.'
    );
  }
  return true;
}

/**
 * Returns a tracer of the SDK provider, or undefined when there is no SDK provider.
 *
 * @param tracerProvider Defaults to the registered provider.
 */
function findSdkTracer(tracerProvider?: TracerProvider): Tracer | undefined {
  const tracer = unwrapProxyProvider(
    tracerProvider ?? trace.getTracerProvider()
  ).getTracer('otel-muzzle');
  return isSdkTracer(tracer) ? tracer : undefined;
}

/** Returns the registered provider behind the API's `ProxyTracerProvider`. */
function unwrapProxyProvider(provider: TracerProvider): TracerProvider {
  const proxy = provider as { getDelegate?: () => TracerProvider };
  return typeof proxy.getDelegate === 'function'
    ? proxy.getDelegate()
    : provider;
}

/**
 * Returns whether the tracer is an SDK tracer. The SDK does not export its `Tracer` class, so this checks for an
 * instrumentation scope, which the API's no-op and proxy tracers lack.
 *
 * @param tracer The tracer to check.
 */
function isSdkTracer(tracer: Tracer): boolean {
  return (
    'instrumentationScope' in tracer &&
    typeof Object.getPrototypeOf(tracer)?.startSpan === 'function'
  );
}

/** Returns whether `OTEL_SDK_DISABLED` is `true`, trimmed and case-insensitive, as the SDK reads it. */
function isSdkDisabled(): boolean {
  return process.env.OTEL_SDK_DISABLED?.trim().toLowerCase() === 'true';
}
