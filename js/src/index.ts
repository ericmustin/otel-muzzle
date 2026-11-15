// SPDX-License-Identifier: Apache-2.0

export { installMuzzle } from './install';
export { MuzzleSampler } from './muzzle-sampler';
export { reload as reloadPolicies } from './policy/runtime';
export type { InstallMuzzleOptions, MuzzleSamplerOptions } from './types';
export type { SpanSamplingImplementer } from './policy/span-sampling';
export type { PolicyApplyStatus } from './policy/model';
