# Contributing to otel-muzzle

otel-muzzle has one package per language, built to one shared design. [docs/design.md](docs/design.md) is the
contract the Ruby, Python and Node.js packages are tested against, so a change in behaviour starts there and then
goes into all three packages.

## Layout

| Path | Contents |
| ---- | -------- |
| `ruby/` | the `otel-muzzle` gem |
| `python/` | the `otel-muzzle` package on PyPI |
| `js/` | the `otel-muzzle` package on npm |
| `docs/design.md` | the cross-language contract |
| `testdata/` | the ratio test vectors every package's tests read, and the script that writes them |
| `examples/` | the sample apps, a compose override for each setup, and the end-to-end verifier `verify.py` |
| `bench/` | the benchmarks |

## Running the tests

Run every command from the repository root unless it says otherwise. The package tests read `testdata/`.
`make test` runs every package's checks, the end-to-end verifier's self-test and the benchmark's unit tests.

### Ruby

Ruby runs only in Docker. `make test-ruby` runs the tests, RuboCop and YARD in a throwaway `ruby:3.3` container:

```sh
make test-ruby                            # against the latest opentelemetry-sdk
make test-ruby RUBY_APPRAISAL=sdk-1.10    # against opentelemetry-sdk 1.10.0, the floor
make test-ruby RUBY_IMAGE=ruby:3.4        # on another Ruby
```

### Python

`make test-python` installs tox in `python/.venv/tox` and runs ruff, mypy and pytest through it:

```sh
make test-python                   # against the latest opentelemetry-api and opentelemetry-sdk
make test-python PY_DEPS=oldest    # against 1.30.0, the floor
```

After that, `cd python && .venv/tox/bin/tox` runs the whole matrix: Python 3.10 to 3.14, on the interpreters it
finds, against both dependency sets.
`python/.pre-commit-config.yaml` pins the same ruff for pre-commit.

### Node.js

In `js/`:

```sh
npm install
npm run lint                # ESLint with Prettier, markdownlint, a compile and publint
npm run lint:fix
npm test                    # mocha under nyc
npm run test:npm-install    # installs the packed package into SDK 2.x, 2.9 and 3.x applications from npm
```

From the repository root, `make test-js` runs `npm ci`, `npm test` and `npm run test:npm-install`. Other SDK lines
and Node.js versions run in Docker:

```sh
make test-js JS_SDK=2.9                    # @opentelemetry/sdk-trace 2.9.0 and api 1.9.0, the floor
make test-js JS_SDK=2 NODE_IMAGE=node:20   # the latest SDK 2.x on Node.js 20
```

### End to end

```sh
make e2e                   # the example stack in Docker, with the SDK set up in code
make e2e MODE=zerocode     # or MODE=operator
make e2e-delegate          # the configured sampler's DROP wins in every language
make bench-quick           # a short run of the OpenTelemetry Demo benchmark
```

[examples/README.md](examples/README.md) describes the stack, the setups and the scenarios the verifier checks.

## License headers

Every source, test and build-script file starts with an SPDX line, with no copyright line and no names:

- Ruby: `# frozen_string_literal: true`, a blank line, then `# SPDX-License-Identifier: Apache-2.0`.
- Python and shell: `# SPDX-License-Identifier: Apache-2.0` on line 1, or on line 2 under a shebang.
- TypeScript and JavaScript: `// SPDX-License-Identifier: Apache-2.0` on line 1.

`make check-spdx`, which `make test` and CI run, enforces the header on every tracked source, test and build file.
ESLint also checks it in `js/`. Files derived from third-party code keep their upstream license and are listed in
`scripts/check-spdx.sh`; today that is `bench/gitlab/opentelemetry.rb`, MIT. `bench/vendor/` holds third-party code: it
gets no header and no other edits.

## Commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), with the package as
the scope: `fix(ruby): …`, `test(js): …`, `build(python): …`. Changes outside the packages use a plain type, as in
`docs: …` or `bench: …`. A user-visible change also gets a one-line entry in the package's `CHANGELOG.md`.

## Test vectors

`testdata/ratio_vectors.json` and `testdata/ratio_env_vectors.json` hold the ratio cases every package's tests check.
`testdata/generate_ratio_vectors.py` writes both, and its output is deterministic. After changing the script,
regenerate the files and commit them with it:

```sh
python3 testdata/generate_ratio_vectors.py
```
