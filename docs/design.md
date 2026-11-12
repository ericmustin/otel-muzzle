# otel-muzzle: design

This is the contract the Ruby, Python and Node.js packages are built and tested against. If a package and this
document disagree, one of them has a bug.

## What it does

otel-muzzle drops INTERNAL spans whose parent is a span in the same process, and keeps each trace one connected
tree: the children of a dropped span attach to its nearest kept ancestor. Everything else stays: SERVER, CLIENT,
PRODUCER and CONSUMER spans, roots, and spans whose parent arrived over the wire.

A share of traces stays **verbose** and keeps every span: 1 in 10 by default (`OTEL_MUZZLE_VERBOSE_RATIO`), plus any
trace whose W3C `tracestate` carries `muzzle=verbose`.

```text
default                                     verbose trace
GET /orders        SERVER                   GET /orders        SERVER
└─ SELECT orders   CLIENT  parent: GET /    └─ load_orders     INTERNAL
                                               └─ SELECT orders   CLIENT
```

## Names

| | Ruby | Python | Node.js |
|---|---|---|---|
| Package | gem `otel-muzzle` | PyPI `otel-muzzle` | npm `otel-muzzle` |
| Load | `require 'otel-muzzle'` (or `'otel/muzzle'`) | `import otel_muzzle` | `require('otel-muzzle')` |
| Sampler | `Otel::Muzzle::Sampler` | `otel_muzzle.MuzzleSampler` | `MuzzleSampler` |
| Patch | `Otel::Muzzle::Patches::Tracer`, prepended onto `OpenTelemetry::SDK::Trace::Tracer` | `opentelemetry.sdk.trace.Tracer.start_span`, replaced by `otel_muzzle._patch` | `startSpan` on the SDK `Tracer` prototype, wrapped by `js/src/patch.ts` |
| Install | `Otel::Muzzle.install!` | `otel_muzzle.install()` | `installMuzzle()` |
| Zero-code hook | instrumentation `OpenTelemetry::Instrumentation::Muzzle` (class `Otel::Muzzle::Instrumentation`), installed by `use` and `use_all` | entry point `opentelemetry_instrumentor: muzzle` (`otel_muzzle.instrumentor.MuzzleInstrumentor`) | none: a setup file loaded with `node --require` |
| Turn off | `OTEL_RUBY_INSTRUMENTATION_MUZZLE_ENABLED=false` | `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=muzzle` | leave `MuzzleSampler` out of the setup file |
| Logs | `OpenTelemetry.logger` | loggers `otel_muzzle` and `otel_muzzle.sampler` | the `diag` component logger `otel-muzzle` |

Shared across all three:

| | |
|---|---|
| Ratio setting | `OTEL_MUZZLE_VERBOSE_RATIO`, default `0.1`. A plain decimal number in ASCII digits, within `[0, 1]`: it must fully match `[+-]?([0-9]+(\.[0-9]+)?\|\.[0-9]+)([eE][+-]?[0-9]+)?`, so `0.1`, `.5` and `1e-1` work and `1.`, hex, `nan`, `inf`, digit separators and non-ASCII digits don't. Surrounding ASCII whitespace is ignored, and an empty value means the default. Anything else logs a warning that names the variable and the bad value (`got "abc"`), then uses `0.1`. |
| Force verbose | tracestate member `muzzle=verbose` (exact, lowercase) |
| Sampler description | `MuzzleSampler{ratio=<ratio>, delegate=<delegate's description>}`, the ratio in the language's own float format (Ruby and Python `1.0`, Node.js `1`) |
| Test vectors | `testdata/ratio_vectors.json` (ratio selection, one `{"trace_id", "ratio", "verbose"}` case per line) and `testdata/ratio_env_vectors.json` (reading `OTEL_MUZZLE_VERBOSE_RATIO`, one `{"raw", "ratio", "warns"}` case per line), both written by `testdata/generate_ratio_vectors.py` |

## Two pieces

1. **The sampler holds all the policy.** It is a plain spec `Sampler` that wraps a *delegate*: whatever sampler
   the application already configured (`OTEL_TRACES_SAMPLER`, the code, or the Operator's `spec.sampler`). The
   default delegate, when one is built from nothing, is `ParentBased(root: AlwaysOn)`. In Node.js it is the sampler
   `OTEL_TRACES_SAMPLER` selects, and `ParentBased(root: AlwaysOn)` only when that is unset: a `NodeSDK` given a
   `sampler` never reads the variable, so the `MuzzleSampler` has to.
2. **A `start_span` patch reparents, and holds no policy.** A sampler cannot reparent. The spec's sampler returns
   only a decision, attributes and a tracestate, and every SDK gives a dropped span a *fresh* span id with
   unsampled flags. Children of that span would point at a parent that is never exported, and with a
   `ParentBased` delegate the whole subtree would be dropped. Only the code that builds the span can hand out the
   parent's identity instead.

Nothing passes between the two: no context key, no flag, no per-call state.

### Sampler

```text
should_sample(parent_context, trace_id, name, kind, attributes, links):
  parent = span_context(current_span(parent_context))
  result = delegate.should_sample(<the same arguments, unchanged>)
  if result is not recording:  return result              # the delegate's DROP always wins
  if keep(parent, trace_id, kind):  return result          # unchanged: same decision, attributes, tracestate
  return DROP with tracestate = result.tracestate, or parent.tracestate when the result has none

keep(parent, trace_id, kind) =
     kind != INTERNAL                                       # a missing kind counts as INTERNAL
  or not parent.valid  or  parent.remote                   # roots and entry spans always stay
  or parent.tracestate["muzzle"] == "verbose"
  or verbose_by_ratio(trace_id)
```

**Ratio.** Let `u` be trace-id bytes 12–15 read as a big-endian unsigned 32-bit integer: the last 8 hex
characters of the 32-character trace id. The trace is verbose when `u < floor(ratio * 2**32)`, so `0` never selects
and `1` always does. Ruby: `trace_id.byteslice(12, 4).unpack1('N')`. Python: `trace_id & 0xFFFFFFFF`. Node.js:
`parseInt(traceId.slice(24, 32), 16)`, after an `isValidTraceId` check, so the all-zero id (which no SDK generates)
is never selected in Node.js. `ratio * 2**32` is exact in double precision, so all three agree.

**Why bytes 12–15:**

- **They are random in every common id scheme.** OpenTelemetry ids are fully random. AWS X-Ray uses epoch seconds,
  then 96 random bits. Datadog's 128-bit ids use epoch seconds, then 4 zero bytes, then 64 random bits. 64-bit
  B3 and Jaeger ids are zero-padded to 128 bits. W3C Trace Context level 2 makes the last 7 bytes random. An
  earlier draft read bytes 4–7, which are zero in Datadog and padded 64-bit ids, so every such trace would have been
  verbose.
- **Head sampling practically never depends on them.**
  - The Ruby and Python `TraceIdRatioBased` samplers compare bytes 8–15 as one number, and the spec's
    consistent-probability samplers compare the low 56 bits. Both comparisons are settled by the leading bytes,
    and bytes 12–15 matter only when those leading bytes equal the threshold's, about once in 4 billion traces.
  - Node.js XORs all four 32-bit words, and the result is independent of bytes 12–15 when bytes 8–11 are random.
  - So 1 in 10 of the traces head sampling keeps is verbose.
- **Every service computes the same answer from the trace id.** A ratio-selected trace is verbose everywhere
  without anything propagating.

`testdata/ratio_vectors.json` (written by `testdata/generate_ratio_vectors.py`) covers both ends, every ratio's
boundary from both sides, and Datadog, padded 64-bit and X-Ray ids.

**Constructor.** `delegate` defaults as above, also when it is an explicit `nil`, `None`, `null` or `undefined`.
`ratio` defaults to `OTEL_MUZZLE_VERBOSE_RATIO` when it is not given, including an explicit `nil`, `None` or
`undefined`. A given ratio that is not a number in `[0, 1]` raises with otel-muzzle's own message:

- NaN, infinity, a string, a boolean, or Node.js `null`;
- "number" is the language's own: Ruby takes any real `Numeric` (`Rational(1, 2)` works, `Complex` doesn't), Python
  `int` or `float` but not `bool`, Node.js a primitive `number`;
- each language uses its usual error, for example Python raises `TypeError` for a non-number and `ValueError` for
  an out-of-range value.

**Default delegate in Node.js.** A `MuzzleSampler` reads `OTEL_TRACES_SAMPLER` when it is built, trimmed and in any
case, as the spec asks of enum values. It takes the six spec names that need no extra package: `always_on`,
`always_off`, `traceidratio` and their `parentbased_` forms. Any other value, `jaeger_remote` and `xray` included, logs
a warning and leaves the default; `NodeSDK` cannot build those from the environment either. For the two ratio
samplers, an `OTEL_TRACES_SAMPLER_ARG` that is missing, blank, not a number or outside `[0, 1]` logs a warning and
means `1`, as in `NodeSDK`. A `TracerProvider` built by hand never reads `OTEL_TRACES_SAMPLER`, but a `MuzzleSampler`
without a `delegate` reads it there too.

### Patch

```text
start_span(...):
  span = original_start_span(...)
  if span is recording:  return span
  ctx  = the parent context the SDK used (none for an explicit root)
  if tracing is suppressed in ctx:  return span            # Ruby untraced?, Node.js isTracingSuppressed (see below)
  parent = span_context(current_span(ctx))
  if not (parent.valid and not parent.remote and parent.sampled):  return span
  return non_recording_span(parent)                         # the parent's own context: ids, flags, tracestate
```

- The rule checks the parent's *flags*, not whether the parent is recording. A replaced span is itself
  non-recording, but it carries its parent's sampled context, so nested drops chain up to the kept ancestor.
- Spans under an unsampled or remote parent, roots, and suppressed spans come back exactly as the SDK built them.
  That includes explicit roots (Python `context=Context()`, Node.js `{ root: true }`), Ruby spans under
  `OpenTelemetry::Common::Utilities.untraced`, and Node.js spans under `suppressTracing()`.
- **Suppression is different in Python.** The Ruby and Node.js SDKs decide suppression inside span creation, and
  the patch preserves that decision. The Python SDK's `Tracer.start_span` never consults `suppress_instrumentation`:
  instrumentations check it themselves and start no span. So there is no SDK decision to preserve, and the Python
  patch has no suppression check. A test pins that SDK behaviour.
- It goes on the SDK's tracer class, so it reaches tracers created before it was applied:
  - Ruby prepends `Otel::Muzzle::Patches::Tracer` onto `OpenTelemetry::SDK::Trace::Tracer`.
  - Python replaces `Tracer.start_span` on the SDK class, and marks the replacement so it is applied once.
  - Node.js wraps `startSpan` on the SDK `Tracer` prototype, and keeps the original under
    `Symbol.for('otel-muzzle.original-startSpan')`, so a second copy of the package sees the patch and does not wrap
    it again.
- Every way into span creation goes through it: Ruby `start_span`/`in_span`, Python
  `start_span`/`start_as_current_span`, Node.js `startSpan`/`startActiveSpan`.
- **Consequence:** if the *delegate* drops a single span under a sampled local parent, that span becomes
  transparent too, and its children attach to the parent instead of disappearing with it. Spec samplers
  (`parentbased_*`, `traceidratio`, `always_*`) never drop a child of a sampled parent, so in practice the patch
  fires only on otel-muzzle's own drops. In Node.js the patch is on the prototype, so this holds for every
  `TracerProvider` of that SDK copy, with or without a `MuzzleSampler`.
- **A dropped span is a placeholder.** Attributes, events, status and exceptions set on it are discarded. Ending
  it does nothing to the parent. In Ruby and Python, an exception raised inside a dropped span's `in_span` or
  `start_as_current_span` block is recorded only if it propagates out through a kept ancestor's block.

### Install

`install` is idempotent, so calling it twice is the same as calling it once. It does three things:

1. It applies the patch once, to the SDK's tracer class or prototype. That way it reaches tracers created
   before the call.
2. It makes sure every SDK tracer samples through a `MuzzleSampler` that wraps the configured sampler, and
   never wraps one twice. Each language does this differently because each SDK stores the sampler differently:
   - **Ruby:** wraps `tracer_provider.sampler`, which every tracer reads per span.
   - **Python:** wraps each tracer's public `sampler` attribute the first time that tracer starts a span, because
     Python tracers copy the provider's sampler when they are created. It leaves `provider.sampler` as it was, so a
     tracer's own `sampler` attribute is the one that shows what it samples with.
   - **Node.js:** cannot swap the sampler, because `NodeSDK` builds the provider and the SDK keeps the sampler in a
     private field. `installMuzzle()` only patches, and logs a warning when the provider's sampler is not a
     `MuzzleSampler`. The application passes `sampler: new MuzzleSampler()` to `NodeSDK` or the `TracerProvider`.
     Only that warning reads the SDK tracer's private `_sampler` field, so an SDK without the field gets no warning
     and works the same. `installMuzzle()` returns `true` once the prototype is patched, by this call or an earlier
     one.
3. If it is called again with an explicit ratio that differs from the installed one, it logs a warning and keeps
   the installed ratio. Node.js `installMuzzle()` takes no ratio: the ratio belongs to the `MuzzleSampler`.
   - An explicit ratio that is invalid raises in Ruby and Python before anything else, also when otel-muzzle is
     already installed or the SDK is disabled.
   - Python leaves a tracer whose sampler is already a `MuzzleSampler` (one the application built) as it is,
     without the warning: the lazy wrap can't tell an earlier `install()` from the application's own choice.

With `OTEL_SDK_DISABLED=true`, install follows the SDK:

- Ruby's `configure` and `NodeSDK` set up no SDK provider, so there is nothing to install onto, and install does
  nothing. A provider the application builds by hand ignores the variable in those two SDKs, and install patches it
  like any other.
- The Python SDK's `TracerProvider` hands out no-op tracers, and `install()` returns without patching.

What happens when install is called before an SDK exists depends on the language:

- **Ruby** raises, because the sampler it wraps does not exist yet. Without the `opentelemetry-sdk` gem at all,
  `require 'otel-muzzle'` prints a warning and defines nothing.
- **Python** works, because the class-level patch and the lazy wrap apply as soon as tracers start spans.
- **Node.js** logs a warning and returns `false`, because it finds the tracer prototype through the registered
  provider. Under `OTEL_SDK_DISABLED=true` no provider is registered either, so it checks that variable only to
  tell the two apart: it returns `false` with a debug line instead of the warning. `NodeSDK` registers a provider
  only when it has a span processor or a trace exporter, so under `OTEL_TRACES_EXPORTER=none` with no span
  processor `installMuzzle()` also returns `false`, with the warning.

`MuzzleInstrumentor`, the Python entry point, calls `install()` from `instrument()` and inherits two rules from
`BaseInstrumentor`:

- `instrument(ratio=…)` takes effect only on the first call. `BaseInstrumentor` ignores `instrument()` while it is
  already instrumented, logging "Attempting to instrument while already instrumented", and never reads the ratio.
  Under `opentelemetry-instrument`, which has already called it, a ratio passed in code is therefore lost, and
  `OTEL_MUZZLE_VERBOSE_RATIO` is the way to set it.
- `uninstrument()` undoes only what `instrument()` installed. After a direct `otel_muzzle.install()` it does
  nothing. After `instrument()` it turns the lazy wrap off: each tracer gets its own sampler back the next time it
  starts a span, and a `MuzzleSampler` the application set stays. The `start_span` patch stays too: it changes
  nothing for a tracer without a `MuzzleSampler`, and the application's own `MuzzleSampler`, or one still in use on
  another thread, needs it to keep traces connected.

## Dependencies

The rule: a package depends only on what it cannot run without, and never pulls in a second copy of
OpenTelemetry.

- **Ruby declares no gem dependencies.** It runs on the application's `opentelemetry-sdk` (>= 1.10). The
  instrumentation's `compatible` block checks that at runtime against `Instrumentation::MINIMUM_VERSION`. It
  registers with `use` and `use_all` when `opentelemetry-instrumentation-base` is loaded, and every instrumentation
  gem and the zero-code loader bring that along. Without that gem, the application calls `Otel::Muzzle.install!`.
  - **Why, under the zero-code loader** (`opentelemetry-auto-instrumentation` 0.1.0, tested 2026-11-07 and 08):
    - **How the loader picks versions.** Installed the default way (`gem install`), the loader's gems share a
      folder with the app's bundle. The loader puts every `opentelemetry-*` folder there on `$LOAD_PATH`, and for
      each gem the folder name that sorts last wins. So a gem the bundle brings can run against the loader's older
      copies of the rest.
    - **`opentelemetry-instrumentation-base` 0.27 breaks traces.** Declaring it made Bundler install 0.27.0.
      - Since 0.27, every instrumentation's `install` creates a meter and a logger before it sets up tracing.
      - Against the loader's metrics API 0.5.0, which lacks `OpenTelemetry::Internal.meter_provider`, that step
        raises.
      - Rack, Net::HTTP, PG and Sinatra never installed, and the app exported no spans.
      - The loader's own copy, 0.26.1, is trace-only.
    - **The trace gems alone don't break traces, and are still left out.** With `opentelemetry-api` 1.11.1 and
      `opentelemetry-sdk` 1.13.1 in the bundle, the loader ran API 1.9.0 with SDK 1.13.1. Every instrumentation
      installed, and otel-muzzle dropped the INTERNAL span and kept the CLIENT span. They are left out anyway, for
      two reasons:
      - under the loader they add nothing;
      - a later SDK that needs a newer API would break there, because the loader keeps API 1.9.0 ("1.9.0" sorts
        after "1.11.1").
    - **The Operator is unaffected.** It keeps the loader's gems in their own folder.
    - **No warning.** The loader warns about `opentelemetry-*` gems in the Gemfile, but it checks only direct
      entries, so gems that arrive through another gem go unflagged.
    - **The Ruby equivalent of a peer dependency.** Ruby has no peer dependencies, so "no dependencies plus a
      runtime version check" stands in for one.
- **Python depends on `opentelemetry-api` and `opentelemetry-sdk` (>= 1.30, < 2).** pip installs one copy per
  environment, so nothing gets mixed.
  - **`opentelemetry-instrumentation` is not a dependency.** Nothing needs it at runtime except the instrumentor
    module. That module is loaded only through the entry point, by `opentelemetry-instrument`, which ships it.
  - **The SDK range is also declared as the `instruments` extra.** `opentelemetry-instrument` and
    `BaseInstrumentor` check it, so the entry point stands down, with a log line, on an unsupported SDK. This
    matters most in Operator images, where otel-muzzle is installed with `--no-deps`. It plays the same role as
    the Ruby `compatible` check.
  - **Why `--no-deps` in an Operator app image.** The Operator's init container copies its own OpenTelemetry
    packages into the pod, and `PYTHONPATH` puts them first. Python looks for entry points on all of `sys.path`, so
    the injected `opentelemetry-instrument` still finds otel-muzzle in the app's `site-packages`, and otel-muzzle
    imports the Operator's SDK, the first one on the path. Without `--no-deps`, pip also installs
    `opentelemetry-api` and `opentelemetry-sdk` into the image, where the Operator's copies shadow them and they go
    unused.
- **Node.js peer-depends on `@opentelemetry/api` (`^1.9.0`).** `@opentelemetry/sdk-trace` is its only optional
  peer, without a version range. It appears only in `peerDependenciesMeta`. Peer dependencies are npm's own way of
  saying "use the application's copy". `@opentelemetry/sdk-node` and `@opentelemetry/sdk-trace-base` depend on
  `@opentelemetry/sdk-trace` from 2.9 on, so an application on the SDK has it.
  - **Why there is no range** (npm 11.5.1, tested 2026-11-08):
    - **When npm resolves a peer range.** It resolves the range, optional or not, to the newest release in range as
      soon as it places otel-muzzle. That happens before it reaches the application's own SDK, one level down
      (`@opentelemetry/sdk-node` 0.223.0 pins `@opentelemetry/sdk-trace` 2.12.0), and it never tries an older
      release in range.
    - **Why that fails on SDK 2.x.** For `^2.9.0 || ^3.0.0` it picks `sdk-trace` 3.1.0, whose
      `@opentelemetry/api` peer (`>=1.10.0 <1.11.0`) conflicts with SDK 2.x's api 1.9.
    - **Where a required peer failed.** Every SDK 2.x application failed with `ERESOLVE`:
      - in a single install command;
      - in a fresh install from `package.json`;
      - when regenerating its lockfile.

      An application with only `"@opentelemetry/api": "^1.9.0"` was instead moved to api 1.10.0 and `sdk-trace`
      3.1.0. Only adding otel-muzzle to an already installed SDK 2.x application worked.
    - **An optional peer with the range** fixed the one-command cases. A fresh install with
      `"@opentelemetry/api": "^1.9.0"` still failed, because npm still weighs optional peers against conflicts.
  - **What it costs.** npm no longer checks the `sdk-trace` version. `js/src/load-sdk-trace.ts` loads it and names
    it when it is missing, such as in an application on an SDK before 2.9, when the package first shipped.
  - **Tests.** `js/test/package.test.ts` pins the peer dependencies. `js/test/integration/npm-install.test.ts`
    resolves the packed tarball into SDK 2.x, 2.9 and 3.x applications three ways: added to an installed app, in one
    command, and from `package.json` with no lockfile.

## Adoption paths

| | Ruby | Python | Node.js |
|---|---|---|---|
| In code | `gem 'otel-muzzle'` + `c.use 'OpenTelemetry::Instrumentation::Muzzle'` or `c.use_all` (or `Otel::Muzzle.install!` after `configure`) | `otel_muzzle.install()` after the provider is set, or pass `sampler=MuzzleSampler(...)` | `sampler: new MuzzleSampler({ delegate })` + `installMuzzle()` |
| Zero-code | `gem 'otel-muzzle'` in the Gemfile, loader via `RUBYOPT` | `pip install otel-muzzle`, run under `opentelemetry-instrument` | none: the standard setup file, `node --require ./instrumentation.js app.js` |
| Operator | same as zero-code: the injected loader calls `Bundler.require` | the package in the app image (or a custom `spec.python.image`) | custom `spec.nodejs.image` whose `autoinstrumentation.js` is the setup file |

The Operator loads the Node.js setup file with `--require`. As with the Operator's own image, the libraries an ES
module application imports are then not instrumented.

## Test contract

Each package's own suite covers these, in that language's test framework:

- **Sampler:**
  - the keep table above, including a missing kind, remote and invalid parents, `muzzle=verbose` versus other
    values (`muzzle=VERBOSE`, `muzzle=quiet`), and a ratio-selected id;
  - all of `testdata/ratio_vectors.json` and `testdata/ratio_env_vectors.json`;
  - the delegate's DROP winning even for a SERVER root;
  - a kept span returning the delegate's result unchanged;
  - the delegate receiving the original arguments;
  - the tracestate fallback;
  - the default delegate;
  - the default, environment and invalid-environment ratio, the last with its warning;
  - invalid ratio arguments, and an explicit null delegate;
  - the description.
- **Patch:**
  - reparenting SERVER → INTERNAL → CLIENT;
  - nested drops chaining up;
  - the dropped span carrying the parent's trace id, span id, flags and tracestate, and not being remote;
  - ending a dropped span leaving the parent recording, with its attributes discarded;
  - unsampled parents, remote parents, roots (including an explicit root option), and suppression, each left
    to the SDK;
  - a custom delegate's drop becoming transparent;
  - verbose traces keeping every span;
  - no change at all without a `MuzzleSampler`;
  - the `start_active`/`in_span` style entry points.
- **Install:**
  - the zero-code path, run for real in a subprocess where that is how the language loads it;
  - the off switch;
  - the environment ratio through zero-code;
  - wrapping an env-selected `traceidratio` sampler;
  - idempotence;
  - reaching tracers created earlier;
  - before an SDK exists, and `OTEL_SDK_DISABLED=true`;
  - the ignored-ratio warning;
  - loading without the optional pieces.

The end-to-end test (`make e2e`, `MODE=code|zerocode|operator`) sends one request through a Ruby, a Node.js and a
Python service, each writing to Postgres and calling the next. `examples/verify.py` checks four scenarios:

1. a sampled `traceparent` whose id the ratio does not select: no INTERNAL spans, CLIENT spans attached to their
   service's SERVER span, one connected tree;
2. the same plus `tracestate: muzzle=verbose`: every span in every service;
3. an id the ratio selects: every span in every service, which shows the three languages agree;
4. no `traceparent`: a connected tree whose verbosity matches what the new id implies.

The benchmark (`bench/`) runs the stock OpenTelemetry Demo services for each language with and without the
package and reports spans and OTLP bytes per request and per trace.

## Upstream facts this relies on

These were checked against opentelemetry-ruby SDK 1.10–1.13, opentelemetry-python SDK 1.30–1.45, and
`@opentelemetry/sdk-trace` 2.9–2.12 and 3.1.

- **Dropped spans:** in all three SDKs a DROP result yields a non-recording span with a fresh span id, unsampled
  flags and the result's tracestate.
- **Propagation:** W3C `inject` writes `tracestate` even from a non-recording span, and `extract` ignores
  `tracestate` without a valid `traceparent`.
- **Ruby:**
  - `TracerProvider#sampler` is an `attr_accessor`, read on every span.
  - The registry installs instrumentation after the provider is set.
  - `option` validators that are callables cannot come from `OTEL_RUBY_INSTRUMENTATION_*_CONFIG_OPTS`.
- **Python:**
  - `Tracer` copies `sampler` at creation.
  - `SamplingResult.attributes` are the span's only initial attributes, so returning the delegate's result
    unchanged keeps them.
  - Instrumentors load after the configurator has set the provider.
  - The W3C propagator discards the whole incoming `tracestate` when any member is invalid or duplicated, so
    `muzzle=verbose` in such a header is lost in a Python entry service but honoured by Ruby and Node.js. Services
    downstream of the entry see one clean `tracestate`.
- **Node.js:**
  - The `Tracer` class is not exported, so the patch goes on `Object.getPrototypeOf(provider.getTracer(...))`.
  - `NodeSDK` cannot take a custom `TracerProvider`, but it does take a `sampler`, and given one it never reads
    `OTEL_TRACES_SAMPLER`.
  - The API logs nothing until a `diag` logger is set, and `NodeSDK` sets one only for `OTEL_LOG_LEVEL`. A
    `MuzzleSampler` is built before `NodeSDK` sets it, so its configuration warnings are queued and logged through
    `diag` on the first sampling decision. They appear only when `OTEL_LOG_LEVEL` is set.
  - `startActiveSpan` calls `this.startSpan`, so patching `startSpan` covers both.
  - `suppressTracing()` sets a context key made with `createContextKey`, which is `Symbol.for`, so the patch reads
    it without depending on `@opentelemetry/core`.
