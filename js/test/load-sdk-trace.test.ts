// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import { spawnSync } from 'node:child_process';
import * as path from 'node:path';
import { PACKAGE_DIR } from './helpers';

describe('the main entry without @opentelemetry/sdk-trace', () => {
  it('should fail with an error that names it', () => {
    const script = `
      const Module = require('node:module');
      const resolve = Module._resolveFilename;
      Module._resolveFilename = function (request, ...rest) {
        if (request === '@opentelemetry/sdk-trace') {
          throw Object.assign(new Error("Cannot find module '" + request + "'"), {
            code: 'MODULE_NOT_FOUND',
          });
        }
        return resolve.call(this, request, ...rest);
      };
      require(${JSON.stringify(path.join(PACKAGE_DIR, 'build/src/index.js'))});
    `;
    const child = spawnSync(process.execPath, ['-e', script], {
      cwd: PACKAGE_DIR,
      encoding: 'utf8',
      timeout: 30_000,
    });
    assert.notStrictEqual(child.status, 0);
    assert.match(
      child.stderr,
      /otel-muzzle needs @opentelemetry\/sdk-trace; upgrade the OpenTelemetry JS SDK to 2\.9 or later/
    );
  });
});
