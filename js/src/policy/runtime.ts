// SPDX-License-Identifier: Apache-2.0

/** Process-wide wiring: one provider, store and implementer per policy file. */
import { realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { logger } from '../utils';
import { FilePolicyProvider } from './file-provider';
import { SpanSamplingImplementer } from './span-sampling';
import { PolicyStore } from './store';

const POLICY_FILE_ENV = 'OTEL_MUZZLE_POLICY_FILE';
const RATIO_ENV = 'OTEL_MUZZLE_VERBOSE_RATIO';

/** The policies of one policy file, and the provider that reads them. Every sampler on that file shares it. */
export class PolicyRuntime {
  public readonly policies: SpanSamplingImplementer;
  public readonly provider: FilePolicyProvider;
  /** Warnings held back until `flushWarnings()`; undefined from then on. */
  private _pendingWarnings: string[] | undefined = [];

  /**
   * Reads the file. A file that cannot be read leaves no policy in effect.
   *
   * @param path The policy file.
   */
  constructor(path: string) {
    const warn = (message: string) => {
      if (this._pendingWarnings === undefined) logger.warn(message);
      else this._pendingWarnings.push(message);
    };
    if ((process.env[RATIO_ENV] ?? '').trim() !== '') {
      warn(
        `${RATIO_ENV} is ignored, because a policy file is configured: ${path}`
      );
    }
    this.policies = new SpanSamplingImplementer(warn);
    this.provider = new FilePolicyProvider(
      path,
      new PolicyStore(this.policies),
      warn
    );
    this.provider.start();
  }

  /**
   * Logs the warnings from reading the file, once; later warnings are logged as they come. A sampler calls this on
   * its first sampling decision: it is built, and the file read, before `NodeSDK` sets the `diag` logger.
   */
  public flushWarnings(): void {
    const warnings = this._pendingWarnings;
    if (warnings === undefined) return;
    this._pendingWarnings = undefined;
    for (const message of warnings) logger.warn(message);
  }
}

const runtimes = new Map<string, PolicyRuntime>();

/** Returns `OTEL_MUZZLE_POLICY_FILE`, or undefined when it is unset or blank. */
export function policyFileFromEnv(
  env: NodeJS.ProcessEnv = process.env
): string | undefined {
  return (env[POLICY_FILE_ENV] ?? '').trim() || undefined;
}

/**
 * Returns the runtime of the file at `path`, which reads the file the first time it is asked for.
 *
 * @param path The policy file. Two paths to one file share a runtime.
 */
export function fromFile(path: string): PolicyRuntime {
  const key = resolvedPath(path);
  let runtime = runtimes.get(key);
  if (runtime === undefined) {
    runtime = new PolicyRuntime(path);
    runtimes.set(key, runtime);
  }
  return runtime;
}

/**
 * Returns the file provider that feeds `policies`, for `reload()`. Throws when `policies` did not come from
 * `fromFile`.
 */
export function providerFor(
  policies: SpanSamplingImplementer
): FilePolicyProvider {
  for (const runtime of runtimes.values()) {
    if (runtime.policies === policies) return runtime.provider;
  }
  throw new Error('otel-muzzle: not the policies of a policy file');
}

/**
 * Reads every policy file in use again.
 *
 * @returns Whether each file loaded. A file that did not keeps the policy set it had.
 */
export function reload(): boolean {
  let loaded = true;
  for (const runtime of runtimes.values()) {
    if (!runtime.provider.reload()) loaded = false;
  }
  return loaded;
}

/** Forgets every file. For tests. */
export function reset(): void {
  runtimes.clear();
}

/** The file's real path, or its absolute path when it does not exist. */
function resolvedPath(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    return resolve(path);
  }
}
