# otel-muzzle for Python

otel-muzzle is an OpenTelemetry Python instrumentation that drops INTERNAL spans whose parent is a local span. The
children of a dropped span attach to its nearest kept ancestor, so every trace stays connected. SERVER, CLIENT,
PRODUCER and CONSUMER spans, root spans and spans with a remote parent are always kept. A share of traces, 10% by
default, stays verbose and keeps every span.

This is an experimental package under active development. New releases may include breaking changes.

## Installation

This package is not yet published to PyPI; install it from the repository:

```sh
pip install "otel-muzzle @ git+https://github.com/ericmustin/otel-muzzle#subdirectory=python"
```

otel-muzzle supports Python 3.10 and later. It depends only on `opentelemetry-api` and `opentelemetry-sdk`, 1.30 or
later and below 2.

## Usage

Call `otel_muzzle.install()` once at startup:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

import otel_muzzle

trace.set_tracer_provider(TracerProvider())
otel_muzzle.install()
```

`install()` works before or after the tracer provider is set, and reaches tracers created before the call. It wraps
the sampler each tracer already has, so `OTEL_TRACES_SAMPLER` and samplers set in code keep working. To build the
sampler yourself, pass a `MuzzleSampler` to the provider and still call `install()`:

```python
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from otel_muzzle import MuzzleSampler

provider = TracerProvider(sampler=MuzzleSampler(ParentBased(TraceIdRatioBased(0.5)), ratio=0.05))
trace.set_tracer_provider(provider)
otel_muzzle.install()
```

### Zero-code instrumentation

otel-muzzle registers the `muzzle` entry point in the `opentelemetry_instrumentor` group, so
[`opentelemetry-instrument`](https://opentelemetry.io/docs/zero-code/python/) installs it after it configures the SDK:

```sh
pip install opentelemetry-distro opentelemetry-exporter-otlp \
  "otel-muzzle @ git+https://github.com/ericmustin/otel-muzzle#subdirectory=python"
opentelemetry-bootstrap -a install
opentelemetry-instrument python app.py
```

### OpenTelemetry Operator

The Operator's Python injection loads the same entry points, with its own copy of the SDK. Install otel-muzzle in the
application image without its dependencies, so that the Operator's copies are the only ones:

```sh
pip install --no-deps "otel-muzzle @ git+https://github.com/ericmustin/otel-muzzle#subdirectory=python"
```

If you cannot change the application image, build your own `spec.python.image`. otel-muzzle is pure Python, so the
same files serve the glibc and the musl directory:

```dockerfile
FROM python:3.14 AS build
RUN pip install --target /otel-muzzle --no-deps \
    "otel-muzzle @ git+https://github.com/ericmustin/otel-muzzle#subdirectory=python"

FROM ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:0.66b1
COPY --from=build /otel-muzzle/ /autoinstrumentation/
COPY --from=build /otel-muzzle/ /autoinstrumentation-musl/
```

## Configuration

| Option | Environment variable | Default | Description |
| ------ | -------------------- | ------- | ----------- |
| `ratio` | `OTEL_MUZZLE_VERBOSE_RATIO` | `0.1` | Share of traces that keep every span: a decimal number in `[0, 1]`, such as `0.05` or `1e-3`. An invalid environment value is logged (logger `otel_muzzle.sampler`) and replaced by `0.1`. |
| `policy_file` | `OTEL_MUZZLE_POLICY_FILE` |  | Experimental. A file of span sampling policies that replaces the built-in rule. See [Policy mode](#policy-mode). |
| `delegate` |  | `ParentBased(ALWAYS_ON)` | The first argument of `MuzzleSampler`: the sampler it wraps. Its decisions stand, and its DROP always wins. |
|  | `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The sampler `install()` wraps, as the SDK builds it. |
|  | `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` |  | `muzzle` stops `opentelemetry-instrument` from installing otel-muzzle. |
|  | `OTEL_SDK_DISABLED` | `false` | `true` makes `install()` do nothing. |

Pass `ratio` to `install()`, to `MuzzleSampler`, or to `MuzzleInstrumentor().instrument()`. A ratio that is not a
number raises `TypeError`, and one outside `[0, 1]` raises `ValueError`. The first `install()` sets the ratio, and a
later call with a different ratio logs a warning (logger `otel_muzzle`) and keeps the first. `BaseInstrumentor`
ignores `instrument()` while it is already instrumented, and `opentelemetry-instrument` has already called it, so
under `opentelemetry-instrument` set `OTEL_MUZZLE_VERBOSE_RATIO` instead. `MuzzleInstrumentor().uninstrument()` undoes
only what `instrument()` installed: after a direct `install()` it does nothing.

## Policy mode

This mode is experimental. It follows a draft of the OpenTelemetry Telemetry Policy specification and will change
with it.

With a policy file, the samplers decide by its `SpanSamplingPolicy` policies instead of the built-in rule, and
`install()` keeps reparenting the children of dropped spans. Set `OTEL_MUZZLE_POLICY_FILE`, which also works under
`opentelemetry-instrument`, or pass the path:

```python
import otel_muzzle

otel_muzzle.install(policy_file="/etc/otel/policy.json")
```

The file is the Protobuf JSON form of a policy set.
[examples/policies/muzzle.json](https://github.com/ericmustin/otel-muzzle/blob/main/examples/policies/muzzle.json)
is the built-in rule written as a policy. In policy mode the ratio is not used: `OTEL_MUZZLE_VERBOSE_RATIO` and a
`ratio` argument are logged and ignored.

The file is read once. A file that cannot be read, or a policy that is not valid, drops nothing and logs a warning
(loggers under `otel_muzzle.policy`). To read the file again while the application runs:

```python
from otel_muzzle import policy

policy.reload()  # False when the file could not be loaded; the previous policies then stay in effect
```

`MuzzleSampler(policy_file=...)` puts one sampler in policy mode, and `MuzzleSampler(policies=...)` takes a
`SpanSamplingImplementer` you load yourself. The
[conformance document](https://github.com/ericmustin/otel-muzzle/blob/main/docs/policy-conformance.md) lists what is
supported and where otel-muzzle differs from the specification.

## Forcing a verbose trace

A request that carries `tracestate: muzzle=verbose` with a valid, sampled `traceparent` is verbose in every service
that runs otel-muzzle, whatever its language:

```sh
curl -H 'traceparent: 00-4bf92f3577b34da6a3ce929d8e0e4736-00f067aa0ba902b7-01' \
     -H 'tracestate: muzzle=verbose' \
     http://localhost:8000/orders
```

Any caller that can set these headers can raise your span volume this way. If untrusted clients reach your
services, remove the `muzzle` member from incoming `tracestate` at the edge.

## References

- [otel-muzzle design](https://github.com/ericmustin/otel-muzzle/blob/main/docs/design.md)
- [Contributing to otel-muzzle](https://github.com/ericmustin/otel-muzzle/blob/main/CONTRIBUTING.md)
- [OpenTelemetry Project](https://opentelemetry.io/)
- [OpenTelemetry Python zero-code instrumentation](https://opentelemetry.io/docs/zero-code/python/)

## License

Apache 2.0. See [LICENSE](https://github.com/ericmustin/otel-muzzle/blob/main/python/LICENSE) for more information.
