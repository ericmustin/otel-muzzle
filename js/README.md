# otel-muzzle for Node.js

otel-muzzle is an OpenTelemetry JS sampler and tracer patch that drops INTERNAL spans whose parent is a local span.
The children of a dropped span attach to its nearest kept ancestor, so every trace stays connected. SERVER, CLIENT,
PRODUCER and CONSUMER spans, root spans and spans with a remote parent are always kept. A share of traces, 10% by
default, stays verbose and keeps every span.

This is an experimental package under active development. New releases may include breaking changes.

## Installation

This package is not yet published to npm; install it from the repository, along with the OpenTelemetry SDK packages
that the Usage setup file loads:

```bash
git clone https://github.com/ericmustin/otel-muzzle
(cd otel-muzzle/js && npm install && npm pack)
npm install ./otel-muzzle/js/otel-muzzle-0.1.0.tgz
npm install @opentelemetry/api @opentelemetry/sdk-node @opentelemetry/auto-instrumentations-node
```

otel-muzzle uses the application's OpenTelemetry packages. `@opentelemetry/api` is a peer dependency, and
`@opentelemetry/sdk-trace` an optional peer that `@opentelemetry/sdk-node` and `@opentelemetry/sdk-trace-base` 2.9
and later bring.

### Supported Versions

- Node.js `^18.19.0 || >=20.6.0`; `@opentelemetry/sdk-trace` 3.x itself needs Node.js 22.15 or later
- `@opentelemetry/api` `^1.9.0`
- `@opentelemetry/sdk-trace` 2.9 and later, including 3.x

## Usage

Write the standard OpenTelemetry JS setup file, with a `MuzzleSampler` in the `NodeSDK`, and call `installMuzzle()`
after `sdk.start()`:

```js
// instrumentation.js
'use strict';

const { NodeSDK } = require('@opentelemetry/sdk-node');
const {
  getNodeAutoInstrumentations,
  getResourceDetectors,
} = require('@opentelemetry/auto-instrumentations-node');
const { MuzzleSampler, installMuzzle } = require('otel-muzzle');

const sdk = new NodeSDK({
  sampler: new MuzzleSampler(),
  instrumentations: getNodeAutoInstrumentations(),
  resourceDetectors: getResourceDetectors(),
});
sdk.start();
installMuzzle();

process.on('SIGTERM', () => {
  sdk.shutdown().finally(() => process.exit(0));
});
```

Load the file before the application:

```bash
node --require ./instrumentation.js app.js
```

For ES modules, see the OpenTelemetry JS
[ESM support](https://github.com/open-telemetry/opentelemetry-js/blob/main/doc/esm-support.md) documentation.

`installMuzzle()` patches the SDK tracer, so the children of dropped spans attach to the nearest kept ancestor. It
returns `false` and logs a warning when no SDK `TracerProvider` is registered; `NodeSDK` registers one only when it has
a span processor or a trace exporter. With a `TracerProvider` of your own, pass it `sampler: new MuzzleSampler()`.

### OpenTelemetry Operator

The Operator loads `autoinstrumentation.js` from its Node.js image with `node --require`. Build an image whose
`autoinstrumentation.js` is the setup file above, and name it in the `Instrumentation` resource:

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: muzzle
spec:
  nodejs:
    image: registry.example.com/autoinstrumentation-nodejs-muzzle:0.1.0
```

[`examples/operator/nodejs`](https://github.com/ericmustin/otel-muzzle/tree/main/examples/operator/nodejs) builds such
an image: the contents of the Operator's own image, plus otel-muzzle. A `spec.sampler` in the resource becomes
`OTEL_TRACES_SAMPLER`, which the `MuzzleSampler` wraps.

## Configuration

| Option | Environment variable | Default | Description |
| ------ | -------------------- | ------- | ----------- |
| `ratio` (`MuzzleSampler`) | `OTEL_MUZZLE_VERBOSE_RATIO` | `0.1` | Share of traces that keep every span: a decimal number in `[0, 1]`, such as `0.05` or `1e-3`. An invalid environment value is logged and replaced by `0.1`. |
| `delegate` (`MuzzleSampler`) | `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The sampler `MuzzleSampler` wraps. Its decisions stand, and its DROP always wins. The variable takes the six spec names that need no extra package. |
| `tracerProvider` (`installMuzzle`) |  | the registered provider | The provider whose SDK tracer is patched. |
|  | `OTEL_LOG_LEVEL` |  | `NodeSDK` sets the `diag` logger from it. Configuration warnings are logged through `diag` on the first sampling decision. |

A `ratio` that is not a number throws a `TypeError`, and one outside `[0, 1]` throws a `RangeError`.

## Forcing a verbose trace

A request that carries `tracestate: muzzle=verbose` with a valid, sampled `traceparent` is verbose in every service
that runs otel-muzzle, whatever its language:

```bash
curl -H 'traceparent: 00-4bf92f3577b34da6a3ce929d8e0e4736-00f067aa0ba902b7-01' \
     -H 'tracestate: muzzle=verbose' \
     http://localhost:3000/orders
```

Any caller that can set these headers can raise your span volume this way. If untrusted clients reach your
services, remove the `muzzle` member from incoming `tracestate` at the edge.

## Useful links

- For more information on OpenTelemetry, visit: <https://opentelemetry.io/>
- For more about OpenTelemetry JavaScript: <https://github.com/open-telemetry/opentelemetry-js>
- For the design otel-muzzle follows in every language: <https://github.com/ericmustin/otel-muzzle/blob/main/docs/design.md>
- For help or feedback, open an issue: <https://github.com/ericmustin/otel-muzzle/issues>

## License

Apache 2.0 - See [LICENSE](https://github.com/ericmustin/otel-muzzle/blob/main/js/LICENSE) for more information.
