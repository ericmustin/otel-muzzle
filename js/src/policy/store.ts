// SPDX-License-Identifier: Apache-2.0

/** The policy store: the one place a provider delivers a policy set to. */
import type {
  InvalidPolicy,
  Policy,
  PolicyApplyStatus,
  PolicyImplementer,
} from './model';

/**
 * Hands each new policy set to the implementer, and keeps the status of every policy in it.
 *
 * A consumer replaces its whole set when it receives a new one: nothing of the previous set stays in effect.
 */
export class PolicyStore {
  private readonly _implementer: PolicyImplementer;
  private _statuses: readonly PolicyApplyStatus[] = [];

  constructor(implementer: PolicyImplementer) {
    this._implementer = implementer;
  }

  /** One status per policy of the set in effect, in document order. */
  public get statuses(): readonly PolicyApplyStatus[] {
    return this._statuses;
  }

  public setPolicies(
    policies: ReadonlyArray<Policy | InvalidPolicy>
  ): readonly PolicyApplyStatus[] {
    this._statuses = this._implementer.applyPolicies(policies);
    return this._statuses;
  }
}
