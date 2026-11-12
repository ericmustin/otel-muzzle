// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import {
  AlwaysOffSampler,
  AlwaysOnSampler,
  ParentBasedSampler,
  TraceIdRatioBasedSampler,
  type Sampler,
} from '@opentelemetry/sdk-trace';
import { samplerFromEnv } from '../src/sampler-from-env';
import { withDiag, withEnv } from './helpers';

type Env = Record<string, string | undefined>;

describe('samplerFromEnv', () => {
  const standard: Array<[Env, Sampler]> = [
    [{ OTEL_TRACES_SAMPLER: 'always_on' }, new AlwaysOnSampler()],
    [{ OTEL_TRACES_SAMPLER: 'always_off' }, new AlwaysOffSampler()],
    [
      { OTEL_TRACES_SAMPLER: 'parentbased_always_on' },
      new ParentBasedSampler({ root: new AlwaysOnSampler() }),
    ],
    [
      { OTEL_TRACES_SAMPLER: 'parentbased_always_off' },
      new ParentBasedSampler({ root: new AlwaysOffSampler() }),
    ],
    [
      { OTEL_TRACES_SAMPLER: 'traceidratio', OTEL_TRACES_SAMPLER_ARG: '0.25' },
      new TraceIdRatioBasedSampler(0.25),
    ],
    [
      {
        OTEL_TRACES_SAMPLER: 'parentbased_traceidratio',
        OTEL_TRACES_SAMPLER_ARG: '0.25',
      },
      new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(0.25) }),
    ],
    [
      { OTEL_TRACES_SAMPLER: 'traceidratio', OTEL_TRACES_SAMPLER_ARG: ' 0 ' },
      new TraceIdRatioBasedSampler(0),
    ],
    [
      { OTEL_TRACES_SAMPLER: 'traceidratio', OTEL_TRACES_SAMPLER_ARG: '1' },
      new TraceIdRatioBasedSampler(1),
    ],
    // Enum values are case-insensitive in the specification.
    [{ OTEL_TRACES_SAMPLER: ' Always_Off ' }, new AlwaysOffSampler()],
    [
      { OTEL_TRACES_SAMPLER: 'PARENTBASED_ALWAYS_ON' },
      new ParentBasedSampler({ root: new AlwaysOnSampler() }),
    ],
  ];
  for (const [env, expected] of standard) {
    it(`should build ${expected.toString()} from ${JSON.stringify(env)}`, () => {
      const [delegate, warnings] = withDiag(() => samplerFromEnv(env));
      assert.ok(delegate instanceof expected.constructor);
      assert.strictEqual(delegate.toString(), expected.toString());
      assert.deepStrictEqual(warnings, []);
    });
  }

  for (const env of [
    {},
    { OTEL_TRACES_SAMPLER: '' },
    { OTEL_TRACES_SAMPLER: '  ' },
    { OTEL_TRACES_SAMPLER_ARG: '0.5' },
  ]) {
    it(`should select nothing, silently, from ${JSON.stringify(env)}`, () => {
      assert.deepStrictEqual(
        withDiag(() => samplerFromEnv(env)),
        [undefined, []]
      );
    });
  }

  for (const name of [
    'jaeger_remote',
    'parentbased_jaeger_remote',
    'xray',
    'sometimes',
  ]) {
    it(`should warn and select nothing for ${name}, which the SDK cannot build from the environment`, () => {
      assert.deepStrictEqual(
        withDiag(() => samplerFromEnv({ OTEL_TRACES_SAMPLER: name })),
        [
          undefined,
          [
            `WARN otel-muzzle unknown OTEL_TRACES_SAMPLER value "${name}", using the default sampler`,
          ],
        ]
      );
    });
  }

  for (const arg of [undefined, '', 'half', '1.5', '-0.1', 'NaN']) {
    it(`should warn and sample everything for a ratio sampler with OTEL_TRACES_SAMPLER_ARG=${JSON.stringify(arg)}, as NodeSDK does`, () => {
      for (const name of ['traceidratio', 'parentbased_traceidratio']) {
        const [delegate, warnings] = withDiag(() =>
          samplerFromEnv({
            OTEL_TRACES_SAMPLER: name,
            OTEL_TRACES_SAMPLER_ARG: arg,
          })
        );
        assert.ok(
          delegate?.toString().includes('TraceIdRatioBased{1}'),
          delegate?.toString()
        );
        assert.deepStrictEqual(warnings, [
          `WARN otel-muzzle OTEL_TRACES_SAMPLER_ARG must be a number within [0, 1], got ${JSON.stringify(arg?.trim() ?? '')}; using 1`,
        ]);
      }
    });
  }

  it('should read process.env by default', () => {
    withEnv({ OTEL_TRACES_SAMPLER: 'always_off' }, () => {
      assert.ok(samplerFromEnv() instanceof AlwaysOffSampler);
    });
  });
});
