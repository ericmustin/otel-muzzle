// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import * as path from 'node:path';
import * as publicApi from '../src/index';
import { PACKAGE_DIR } from './helpers';

const manifest = JSON.parse(
  readFileSync(path.join(PACKAGE_DIR, 'package.json'), 'utf8')
);

describe('otel-muzzle package', () => {
  it('should export MuzzleSampler, installMuzzle and reloadPolicies, and nothing else', () => {
    assert.deepStrictEqual(Object.keys(publicApi).sort(), [
      'MuzzleSampler',
      'installMuzzle',
      'reloadPolicies',
    ]);
  });

  it('should take OpenTelemetry from the application: the API as a peer, sdk-trace as an optional peer', () => {
    assert.strictEqual(manifest.dependencies, undefined);
    assert.deepStrictEqual(manifest.peerDependencies, {
      '@opentelemetry/api': '^1.9.0',
    });
    // @opentelemetry/sdk-trace is an optional peer with no version range. npm resolves a peer range, optional or
    // not, to the newest release in range as soon as it places otel-muzzle, before it reaches the application's own
    // SDK: for @opentelemetry/sdk-trace that is 3.x, whose @opentelemetry/api peer (>= 1.10) fails with ERESOLVE
    // against an SDK 2.x application's api 1.9. With no range, npm uses the application's copy. src/load-sdk-trace.ts
    // names a missing @opentelemetry/sdk-trace at runtime instead.
    assert.deepStrictEqual(manifest.peerDependenciesMeta, {
      '@opentelemetry/sdk-trace': { optional: true },
    });
  });

  it('should have every exports target, and resolve through its own exports', () => {
    const targets: string[] = [];
    const collect = (value: unknown): void => {
      if (typeof value === 'string') targets.push(value);
      else if (value && typeof value === 'object')
        Object.values(value).forEach(collect);
    };
    collect(manifest.exports);
    for (const target of targets)
      assert.ok(existsSync(path.join(PACKAGE_DIR, target)), target);
    assert.strictEqual(
      require.resolve('otel-muzzle'),
      path.join(PACKAGE_DIR, 'build/src/index.js')
    );
  });

  it('should pack only the manifest, the docs, the license and build/src', () => {
    // --ignore-scripts: prepack would rebuild (and first clean) the build these tests run from.
    const output = execFileSync(
      'npm',
      ['pack', '--dry-run', '--json', '--ignore-scripts'],
      { cwd: PACKAGE_DIR, encoding: 'utf8' }
    );
    const paths = (JSON.parse(output)[0].files as Array<{ path: string }>).map(
      file => file.path
    );
    const topLevel = ['package.json', 'README.md', 'LICENSE', 'CHANGELOG.md'];
    for (const file of paths)
      assert.ok(topLevel.includes(file) || file.startsWith('build/src/'), file);
    assert.ok(paths.includes('build/src/index.js'));
    assert.deepStrictEqual(
      paths.filter(file => path.basename(file).includes('register')),
      []
    );
    assert.deepStrictEqual(
      paths.filter(file => file.startsWith('build/test/')),
      []
    );
  });
});
