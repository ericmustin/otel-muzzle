// SPDX-License-Identifier: Apache-2.0

import * as assert from 'assert';
import {
  copyFileSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { SpanKind, TraceFlags, type SpanContext } from '@opentelemetry/api';
import { FilePolicyProvider } from '../src/policy/file-provider';
import { parsePolicySet } from '../src/policy/parser';
import {
  fromFile,
  policyFileFromEnv,
  providerFor,
  reload,
  reset,
} from '../src/policy/runtime';
import { SpanSamplingImplementer } from '../src/policy/span-sampling';
import { PolicyStore } from '../src/policy/store';
import {
  EXAMPLE_POLICY,
  LOCAL_SPAN_ID,
  POLICY_DROP_TRACE_ID,
  withDiag,
  withEnv,
} from './helpers';

const LOCAL: SpanContext = {
  traceId: POLICY_DROP_TRACE_ID,
  spanId: LOCAL_SPAN_ID,
  traceFlags: TraceFlags.SAMPLED,
};
const DROP_ALL =
  '{"policies": [{"id": "none", "spanSampling": {"sampling": {"percentage": 0}}}]}';
const KEEP_ALL =
  '{"policies": [{"id": "all", "spanSampling": {"sampling": {"percentage": 100}}}]}';
const NOT_APPLIED = '{"policies": [{"id": "logs", "logFilter": {}}]}';
const NO_ENV = {
  OTEL_MUZZLE_POLICY_FILE: undefined,
  OTEL_MUZZLE_VERBOSE_RATIO: undefined,
};

let directory: string;
/** A copy of the example policy, in a directory of its own. */
let policyFile: string;
let warnings: string[];
const warn = (message: string) => void warnings.push(message);

/** Whether the policies keep an INTERNAL span under a local parent, in a trace at randomness 0. */
function kept(policies: SpanSamplingImplementer): boolean {
  return policies.keep(
    POLICY_DROP_TRACE_ID,
    'work',
    SpanKind.INTERNAL,
    LOCAL,
    undefined,
    undefined
  );
}

function useTemporaryPolicyFile(): void {
  beforeEach(() => {
    directory = mkdtempSync(path.join(os.tmpdir(), 'otel-muzzle-'));
    policyFile = path.join(directory, 'policy.json');
    copyFileSync(EXAMPLE_POLICY, policyFile);
    warnings = [];
  });
  afterEach(() => {
    rmSync(directory, { recursive: true, force: true });
  });
}

describe('policy store', () => {
  it('should replace the whole set, and keep one status per policy', () => {
    const policies = new SpanSamplingImplementer();
    const store = new PolicyStore(policies);
    assert.deepStrictEqual(store.statuses, []);
    assert.deepStrictEqual(store.setPolicies(parsePolicySet(DROP_ALL)), [
      { policyId: 'none', applied: true },
    ]);
    assert.deepStrictEqual(store.statuses, [
      { policyId: 'none', applied: true },
    ]);
    assert.ok(!kept(policies));
    store.setPolicies(parsePolicySet(KEEP_ALL));
    assert.deepStrictEqual(store.statuses, [
      { policyId: 'all', applied: true },
    ]);
    assert.ok(kept(policies));
  });
});

describe('policy file provider', () => {
  useTemporaryPolicyFile();

  it('should load the file at start', () => {
    const policies = new SpanSamplingImplementer(warn);
    const store = new PolicyStore(policies);
    const provider = new FilePolicyProvider(policyFile, store, warn);
    assert.strictEqual(provider.path, policyFile);
    assert.ok(kept(policies));
    provider.start();
    assert.deepStrictEqual(store.statuses, [
      { policyId: 'drop-internal-spans', applied: true },
    ]);
    assert.ok(!kept(policies));
    assert.deepStrictEqual(warnings, []);
    provider.shutdown();
    assert.ok(!kept(policies));
  });

  it('should drop nothing and warn once for a file it cannot load', () => {
    const contents: Array<[string, string | Buffer | undefined]> = [
      ['a missing file', undefined],
      ['an empty file', ''],
      ['truncated JSON', '{'],
      ['a JSON array', '[]'],
      ['bytes that are not UTF-8', Buffer.from([0xff, 0xfe])],
      [
        'a string that is not UTF-8',
        Buffer.concat([
          Buffer.from('{"policies": [{"id": "'),
          Buffer.from([0xc3, 0x28]),
          Buffer.from('", "spanSampling": {"sampling": {"percentage": 0}}}]}'),
        ]),
      ],
      ['a byte order mark', `\ufeff${DROP_ALL}`],
    ];
    for (const [label, content] of contents) {
      warnings = [];
      const file = path.join(directory, 'unreadable.json');
      rmSync(file, { force: true });
      if (content !== undefined) writeFileSync(file, content);
      const policies = new SpanSamplingImplementer(warn);
      const store = new PolicyStore(policies);
      new FilePolicyProvider(file, store, warn).start();
      assert.ok(kept(policies), label);
      assert.deepStrictEqual(store.statuses, [], label);
      assert.strictEqual(warnings.length, 1, label);
      assert.ok(
        warnings[0].startsWith(`cannot load the policy file ${file}: `) &&
          warnings[0].endsWith('; no policy is in effect'),
        `${label}: ${warnings[0]}`
      );
    }
  });

  it('should say why a file cannot be loaded', () => {
    const load = (file: string): string => {
      warnings = [];
      new FilePolicyProvider(
        file,
        new PolicyStore(new SpanSamplingImplementer(warn)),
        warn
      ).start();
      return warnings[0];
    };
    assert.match(load(path.join(directory, 'missing.json')), /: ENOENT: /);
    assert.match(load(directory), /: EISDIR: /);
    writeFileSync(policyFile, '{"policies": {}}');
    assert.strictEqual(
      load(policyFile),
      `cannot load the policy file ${policyFile}: policies must be a list; no policy is in effect`
    );
    writeFileSync(policyFile, 'policies: []');
    assert.match(
      load(policyFile),
      /: not valid JSON: .*; no policy is in effect$/
    );
  });

  it('should empty the store when start finds no policy set', () => {
    const policies = new SpanSamplingImplementer(warn);
    const store = new PolicyStore(policies);
    store.setPolicies(parsePolicySet(DROP_ALL));
    new FilePolicyProvider(
      path.join(directory, 'missing.json'),
      store,
      warn
    ).start();
    assert.deepStrictEqual(store.statuses, []);
    assert.ok(kept(policies));
  });

  it('should log through diag by default', () => {
    const file = path.join(directory, 'missing.json');
    const [, messages] = withDiag(() =>
      new FilePolicyProvider(
        file,
        new PolicyStore(new SpanSamplingImplementer())
      ).start()
    );
    assert.strictEqual(messages.length, 1);
    assert.ok(
      messages[0].startsWith(
        `WARN otel-muzzle cannot load the policy file ${file}: ENOENT`
      ),
      messages[0]
    );
  });

  it('should swap the set on reload', () => {
    const policies = new SpanSamplingImplementer(warn);
    const store = new PolicyStore(policies);
    const provider = new FilePolicyProvider(policyFile, store, warn);
    provider.start();
    assert.ok(!kept(policies));
    writeFileSync(policyFile, KEEP_ALL);
    assert.strictEqual(provider.reload(), true);
    assert.ok(kept(policies));
    assert.deepStrictEqual(store.statuses, [
      { policyId: 'all', applied: true },
    ]);
    assert.deepStrictEqual(warnings, []);
  });

  it('should keep the previous set when a reload fails', () => {
    const policies = new SpanSamplingImplementer(warn);
    const store = new PolicyStore(policies);
    const provider = new FilePolicyProvider(policyFile, store, warn);
    provider.start();
    writeFileSync(policyFile, '{');
    assert.strictEqual(provider.reload(), false);
    assert.ok(!kept(policies));
    assert.deepStrictEqual(store.statuses, [
      { policyId: 'drop-internal-spans', applied: true },
    ]);
    assert.strictEqual(warnings.length, 1);
    assert.ok(
      warnings[0].startsWith(`cannot load the policy file ${policyFile}: `) &&
        warnings[0].endsWith('; keeping the previous policy set'),
      warnings[0]
    );
    unlinkSync(policyFile);
    assert.strictEqual(provider.reload(), false);
    assert.ok(!kept(policies));
    assert.strictEqual(warnings.length, 2);
  });
});

describe('policy runtime', () => {
  useTemporaryPolicyFile();
  beforeEach(() => reset());
  afterEach(() => reset());

  it('should hold one runtime per file', () => {
    withEnv(NO_ENV, () => {
      const first = fromFile(policyFile);
      assert.strictEqual(fromFile(policyFile), first);
      assert.strictEqual(
        fromFile(`${directory}/./${path.basename(policyFile)}`),
        first
      );
      assert.strictEqual(
        fromFile(path.relative(process.cwd(), policyFile)),
        first
      );
      const link = path.join(directory, 'link.json');
      symlinkSync(policyFile, link);
      assert.strictEqual(fromFile(link), first);
      const other = path.join(directory, 'other.json');
      writeFileSync(other, KEEP_ALL);
      assert.notStrictEqual(fromFile(other), first);
      assert.ok(!kept(first.policies));
      assert.ok(kept(fromFile(other).policies));
      assert.strictEqual(first.provider.path, policyFile);
    });
  });

  it('should hold one runtime for a file that does not exist', () => {
    withEnv(NO_ENV, () => {
      const missing = path.join(directory, 'missing.json');
      const first = fromFile(missing);
      assert.strictEqual(fromFile(`${directory}/./missing.json`), first);
      assert.ok(kept(first.policies) && !first.policies.active);
    });
  });

  it('should forget every file on reset', () => {
    withEnv(NO_ENV, () => {
      const first = fromFile(policyFile);
      reset();
      assert.notStrictEqual(fromFile(policyFile), first);
    });
  });

  it('should read the policy file from OTEL_MUZZLE_POLICY_FILE, and a blank value as unset', () => {
    for (const [raw, expected] of [
      [undefined, undefined],
      ['', undefined],
      ['  ', undefined],
      [' /etc/policy.json ', '/etc/policy.json'],
    ] as const) {
      withEnv({ OTEL_MUZZLE_POLICY_FILE: raw }, () => {
        assert.strictEqual(policyFileFromEnv(), expected);
      });
    }
    assert.strictEqual(
      policyFileFromEnv({ OTEL_MUZZLE_POLICY_FILE: 'a.json' }),
      'a.json'
    );
  });

  it('should hold its warnings until they are flushed, then log as they come', () => {
    withEnv(NO_ENV, () => {
      writeFileSync(policyFile, NOT_APPLIED);
      const [runtime, atStart] = withDiag(() => fromFile(policyFile));
      assert.deepStrictEqual(atStart, []);
      const [, flushed] = withDiag(() => {
        runtime.flushWarnings();
        runtime.flushWarnings();
      });
      assert.deepStrictEqual(flushed, [
        "WARN otel-muzzle policy 'logs' is not applied: unsupported policy type: log_filter",
      ]);
      const [, later] = withDiag(() => runtime.provider.reload());
      assert.deepStrictEqual(later, flushed);
    });
  });

  it('should warn once that OTEL_MUZZLE_VERBOSE_RATIO is ignored', () => {
    withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: '0.5' }, () => {
      const [, messages] = withDiag(() => {
        fromFile(policyFile).flushWarnings();
        fromFile(policyFile).flushWarnings();
      });
      assert.deepStrictEqual(messages, [
        `WARN otel-muzzle OTEL_MUZZLE_VERBOSE_RATIO is ignored, because a policy file is configured: ${policyFile}`,
      ]);
    });
    for (const blank of [undefined, '', '  ']) {
      reset();
      withEnv({ ...NO_ENV, OTEL_MUZZLE_VERBOSE_RATIO: blank }, () => {
        const [, messages] = withDiag(() =>
          fromFile(policyFile).flushWarnings()
        );
        assert.deepStrictEqual(messages, []);
      });
    }
  });

  it('should find the provider that feeds the policies of a file, for a reload', () => {
    withEnv(NO_ENV, () => {
      const { policies, provider } = fromFile(policyFile);
      assert.strictEqual(providerFor(policies), provider);
      writeFileSync(policyFile, KEEP_ALL);
      assert.strictEqual(providerFor(policies).reload(), true);
      assert.ok(kept(policies));
      assert.throws(
        () => providerFor(new SpanSamplingImplementer()),
        /not the policies of a policy file/
      );
    });
  });

  it('should read every file in use again on reload', () => {
    withEnv(NO_ENV, () => {
      const other = path.join(directory, 'other.json');
      writeFileSync(other, KEEP_ALL);
      const first = fromFile(policyFile).policies;
      const second = fromFile(other).policies;
      writeFileSync(policyFile, KEEP_ALL);
      writeFileSync(other, DROP_ALL);
      assert.strictEqual(reload(), true);
      assert.ok(kept(first));
      assert.ok(!kept(second));
    });
  });

  it('should report a file that failed on reload, and still read the others', () => {
    withEnv(NO_ENV, () => {
      const other = path.join(directory, 'other.json');
      writeFileSync(other, KEEP_ALL);
      const first = fromFile(policyFile);
      const second = fromFile(other).policies;
      first.flushWarnings();
      writeFileSync(policyFile, 'not json');
      writeFileSync(other, DROP_ALL);
      const [result, messages] = withDiag(() => reload());
      assert.strictEqual(result, false);
      assert.ok(!kept(first.policies));
      assert.ok(!kept(second));
      assert.strictEqual(messages.length, 1);
      assert.match(messages[0], /keeping the previous policy set$/);
    });
  });

  it('should have nothing to reload without a policy file', () => {
    assert.strictEqual(reload(), true);
  });
});
