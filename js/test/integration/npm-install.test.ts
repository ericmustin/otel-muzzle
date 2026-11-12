// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import { execFile } from 'node:child_process';
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { promisify } from 'node:util';
import { PACKAGE_DIR } from '../helpers';

// How npm resolves the packed otel-muzzle into applications on each SDK line (`npm install --package-lock-only`):
// no ERESOLVE, and one copy of @opentelemetry/api and of @opentelemetry/sdk-trace, the application's. Covers adding
// otel-muzzle to an installed application, installing it in the same command as the SDK, and installing an
// application from its package.json with no lockfile (a new checkout, or a regenerated lockfile). The matrix in
// `make test-js` installs each SDK into this package's own tree, which cannot show any of this. Needs the npm
// registry, so it runs as `npm run test:npm-install`, apart from `npm test`.

const run = promisify(execFile);
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm';

const { registry } = JSON.parse(
  readFileSync(path.join(PACKAGE_DIR, 'test/sdk-versions.json'), 'utf8')
) as { registry: Record<'sdk2' | 'sdk2.9' | 'sdk3', string[]> };
const SDK_2 = registry.sdk2;
const SDK_2_9 = registry['sdk2.9'];
const SDK_3 = registry.sdk3;

describe('npm install otel-muzzle into an application', () => {
  let dir = '';
  let tarball = '';

  before(async () => {
    dir = mkdtempSync(path.join(tmpdir(), 'otel-muzzle-npm-'));
    const { stdout } = await run(
      NPM,
      ['pack', '--ignore-scripts', '--json', '--pack-destination', dir],
      { cwd: PACKAGE_DIR }
    );
    tarball = path.join(
      dir,
      (JSON.parse(stdout) as Array<{ filename: string }>)[0]!.filename
    );
  });

  after(() => rmSync(dir, { recursive: true, force: true }));

  /** Runs each install in a new application ('otel-muzzle' means the packed tarball); returns the locked versions. */
  async function resolve(
    name: string,
    ...installs: string[][]
  ): Promise<Record<string, string[]>> {
    const app = newApp(name, {});
    for (const install of installs) {
      const specs = install.map(spec =>
        spec === 'otel-muzzle' ? tarball : spec
      );
      await run(
        NPM,
        ['install', '--package-lock-only', '--no-audit', '--no-fund', ...specs],
        { cwd: app }
      );
    }
    return locked(app);
  }

  /** Installs a new application from its package.json alone, with otel-muzzle added; returns the locked versions. */
  async function resolveFresh(
    name: string,
    dependencies: Record<string, string>
  ): Promise<Record<string, string[]>> {
    const app = newApp(name, {
      ...dependencies,
      'otel-muzzle': `file:${tarball}`,
    });
    await run(
      NPM,
      ['install', '--package-lock-only', '--no-audit', '--no-fund'],
      { cwd: app }
    );
    return locked(app);
  }

  function newApp(name: string, dependencies: Record<string, string>): string {
    const app = path.join(dir, name);
    mkdirSync(app);
    writeFileSync(
      path.join(app, 'package.json'),
      JSON.stringify({ name, version: '1.0.0', private: true, dependencies })
    );
    return app;
  }

  /** The locked versions of @opentelemetry/api and @opentelemetry/sdk-trace, one entry per copy. */
  function locked(app: string): Record<string, string[]> {
    const lock = JSON.parse(
      readFileSync(path.join(app, 'package-lock.json'), 'utf8')
    ) as {
      packages: Record<string, { version: string }>;
    };
    const versions: Record<string, string[]> = {};
    for (const [key, pkg] of Object.entries(lock.packages)) {
      const match = /node_modules\/(@opentelemetry\/(?:api|sdk-trace))$/.exec(
        key
      );
      if (match) (versions[match[1]!] ??= []).push(pkg.version);
    }
    return versions;
  }

  it('should use the api and sdk-trace of an SDK 2.x application', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(await resolve('sdk2', SDK_2, ['otel-muzzle']), {
      '@opentelemetry/api': ['1.9.0'],
      '@opentelemetry/sdk-trace': ['2.12.0'],
    });
  });

  it('should use the api and sdk-trace of an SDK 2.x application built on sdk-trace-base', async function () {
    this.timeout(120_000);
    const base = [
      '@opentelemetry/api@1.9.1',
      '@opentelemetry/sdk-trace-base@2.10.0',
    ];
    assert.deepStrictEqual(await resolve('sdk2-base', base, ['otel-muzzle']), {
      '@opentelemetry/api': ['1.9.1'],
      '@opentelemetry/sdk-trace': ['2.10.0'],
    });
  });

  it('should use the api and sdk-trace of an application on the oldest supported SDK', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(await resolve('sdk2.9', SDK_2_9, ['otel-muzzle']), {
      '@opentelemetry/api': ['1.9.0'],
      '@opentelemetry/sdk-trace': ['2.9.0'],
    });
  });

  it('should use the api and sdk-trace of SDK 2.x installed in the same command', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(
      await resolve('sdk2-together', ['otel-muzzle', ...SDK_2]),
      {
        '@opentelemetry/api': ['1.9.0'],
        '@opentelemetry/sdk-trace': ['2.12.0'],
      }
    );
  });

  it('should use the api and sdk-trace of the oldest supported SDK installed in the same command', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(
      await resolve('sdk2.9-together', ['otel-muzzle', ...SDK_2_9]),
      {
        '@opentelemetry/api': ['1.9.0'],
        '@opentelemetry/sdk-trace': ['2.9.0'],
      }
    );
  });

  it('should use the api and sdk-trace of SDK 2.x sdk-trace-base installed in the same command', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(
      await resolve('sdk2-base-together', [
        'otel-muzzle',
        '@opentelemetry/api@1.9.1',
        '@opentelemetry/sdk-trace-base@2.10.0',
      ]),
      {
        '@opentelemetry/api': ['1.9.1'],
        '@opentelemetry/sdk-trace': ['2.10.0'],
      }
    );
  });

  it('should use the api and sdk-trace of an SDK 2.x application installed from its package.json, with no lockfile', async function () {
    this.timeout(120_000);
    const dependencies = {
      '@opentelemetry/api': '^1.9.0',
      '@opentelemetry/sdk-node': '0.223.0',
    };
    assert.deepStrictEqual(await resolveFresh('sdk2-fresh', dependencies), {
      '@opentelemetry/api': ['1.9.1'],
      '@opentelemetry/sdk-trace': ['2.12.0'],
    });
  });

  it('should use the api and sdk-trace of an SDK 3.x application installed from its package.json, with no lockfile', async function () {
    this.timeout(120_000);
    const dependencies = {
      '@opentelemetry/api': '^1.10.0',
      '@opentelemetry/sdk-node': '0.301.0',
    };
    assert.deepStrictEqual(await resolveFresh('sdk3-fresh', dependencies), {
      '@opentelemetry/api': ['1.10.0'],
      '@opentelemetry/sdk-trace': ['3.1.0'],
    });
  });

  it('should leave @opentelemetry/sdk-trace to the SDK in an application with only the API', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(
      await resolve('api-only', ['@opentelemetry/api@1.9.0'], ['otel-muzzle']),
      {
        '@opentelemetry/api': ['1.9.0'],
      }
    );
  });

  it('should use the api and sdk-trace of an SDK 3.x application', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(await resolve('sdk3', SDK_3, ['otel-muzzle']), {
      '@opentelemetry/api': ['1.10.0'],
      '@opentelemetry/sdk-trace': ['3.1.0'],
    });
  });

  it('should use the api and sdk-trace of SDK 3.x installed in the same command', async function () {
    this.timeout(120_000);
    assert.deepStrictEqual(
      await resolve('sdk3-together', ['otel-muzzle', ...SDK_3]),
      {
        '@opentelemetry/api': ['1.10.0'],
        '@opentelemetry/sdk-trace': ['3.1.0'],
      }
    );
  });
});
