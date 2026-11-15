// SPDX-License-Identifier: Apache-2.0

/** The file policy provider: a policy set read from a JSON file. */
import { readFileSync } from 'node:fs';
import { logger } from '../utils';
import type { InvalidPolicy, Policy } from './model';
import { parsePolicySet } from './parser';
import type { PolicyStore } from './store';

/**
 * Reads a policy file into a store: once at `start()`, and again whenever `reload()` is called. It reads
 * synchronously, because a sampler is built synchronously.
 *
 * It fails open. A file that cannot be read or is not a policy set leaves no policy in effect at start, and leaves
 * the previous set in effect on a reload. Either way a warning names the file.
 */
export class FilePolicyProvider {
  public readonly path: string;
  private readonly _store: PolicyStore;
  private readonly _warn: (message: string) => void;

  /**
   * @param path The policy file.
   * @param store Receives each policy set the file holds.
   * @param warn Receives each warning. Defaults to the component logger.
   */
  constructor(
    path: string,
    store: PolicyStore,
    warn: (message: string) => void = message => logger.warn(message)
  ) {
    this.path = path;
    this._store = store;
    this._warn = warn;
  }

  public start(): void {
    if (!this._load('no policy is in effect')) this._store.setPolicies([]);
  }

  /** Reads the file again. Returns whether its policy set is now the one in effect. */
  public reload(): boolean {
    return this._load('keeping the previous policy set');
  }

  /** Nothing to release: the provider holds no timer and no open file. */
  public shutdown(): void {}

  private _load(otherwise: string): boolean {
    let policies: Array<Policy | InvalidPolicy>;
    try {
      // Strict UTF-8, with a byte order mark left in place for the parser to reject: a policy file is plain JSON.
      const text = new TextDecoder('utf-8', {
        fatal: true,
        ignoreBOM: true,
      }).decode(readFileSync(this.path));
      policies = parsePolicySet(text);
    } catch (error) {
      const reason = (error as Error).message;
      this._warn(
        `cannot load the policy file ${this.path}: ${reason}; ${otherwise}`
      );
      return false;
    }
    this._store.setPolicies(policies);
    return true;
  }
}
