# otel-muzzle for Ruby

otel-muzzle is an OpenTelemetry Ruby instrumentation that drops INTERNAL spans whose parent is a local span. The
children of a dropped span attach to its nearest kept ancestor, so every trace stays connected. SERVER, CLIENT,
PRODUCER and CONSUMER spans, root spans and spans with a remote parent are always kept. A share of traces, 10% by
default, stays verbose and keeps every span.

This is an experimental package under active development. New releases may include breaking changes.

## How do I get started?

This package is not yet published to RubyGems; install it from the repository. With [bundler](https://bundler.io),
add this line to your `Gemfile`:

```ruby
gem 'otel-muzzle', git: 'https://github.com/ericmustin/otel-muzzle', glob: 'ruby/*.gemspec'
```

### Version Compatibility

otel-muzzle declares no gem dependencies. It runs on the OpenTelemetry gems the application already loads.

| Dependency | Supported versions |
| ---------- | ------------------ |
| Ruby | `>= 3.3` |
| `opentelemetry-sdk` | `>= 1.10.0` |
| `opentelemetry-instrumentation-base` | `>= 0.26`, for `use` and `use_all` |

## Usage

To use the instrumentation, require the gem and call `use` with the name of the instrumentation:

```ruby
require 'opentelemetry/sdk'
require 'otel-muzzle'

OpenTelemetry::SDK.configure do |c|
  c.use 'OpenTelemetry::Instrumentation::Muzzle'
end
```

Alternatively, call `use_all` to install all the available instrumentation:

```ruby
OpenTelemetry::SDK.configure do |c|
  c.use_all
end
```

otel-muzzle wraps the sampler the tracer provider already has, so `OTEL_TRACES_SAMPLER` and samplers set in code keep
working. Without `opentelemetry-instrumentation-base`, call `Otel::Muzzle.install!` after `OpenTelemetry::SDK.configure`.

### Zero-code instrumentation and the OpenTelemetry Operator

The [zero-code loader](https://github.com/open-telemetry/opentelemetry-ruby-instrumentation) and the OpenTelemetry
Operator's Ruby injection call `use_all` while `Bundler.require` runs. Add the `gem 'otel-muzzle'` line from
[How do I get started?](#how-do-i-get-started) to the application's `Gemfile`, and nothing else. Applications that
do not call `Bundler.require` themselves set `OTEL_RUBY_REQUIRE_BUNDLER=true`, as with any zero-code setup. If you
set `OTEL_RUBY_ENABLED_INSTRUMENTATIONS`, include `muzzle` in the list.

## Configuration

| Option | Environment variable | Default | Description |
| ------ | -------------------- | ------- | ----------- |
| `ratio` | `OTEL_MUZZLE_VERBOSE_RATIO` | `0.1` | Share of traces that keep every span: a decimal number in `[0, 1]`, such as `0.05` or `1e-3`. An invalid environment value is logged and replaced by `0.1`. |
|  | `OTEL_RUBY_INSTRUMENTATION_MUZZLE_ENABLED` | `true` | `false` stops `use` and `use_all` from installing otel-muzzle. |
|  | `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The sampler otel-muzzle wraps. Its decisions stand, and its DROP always wins. |

Pass the `ratio` option the usual way:

```ruby
OpenTelemetry::SDK.configure do |c|
  c.use 'OpenTelemetry::Instrumentation::Muzzle', ratio: 0.05
end
```

`OTEL_RUBY_INSTRUMENTATION_MUZZLE_CONFIG_OPTS` cannot carry `ratio`; set `OTEL_MUZZLE_VERBOSE_RATIO` instead.
`Otel::Muzzle.install!(ratio:)` and `Otel::Muzzle::Sampler.new(delegate:, ratio:)` take the same value, and raise
`ArgumentError` for anything that is not a number in `[0, 1]`. The first install sets the ratio. A later
`install!` with a different ratio logs a warning and keeps the first.

## Forcing a verbose trace

A request that carries `tracestate: muzzle=verbose` with a valid, sampled `traceparent` is verbose in every service
that runs otel-muzzle, whatever its language:

```sh
curl -H 'traceparent: 00-4bf92f3577b34da6a3ce929d8e0e4736-00f067aa0ba902b7-01' \
     -H 'tracestate: muzzle=verbose' \
     http://localhost:4567/orders
```

Any caller that can set these headers can raise your span volume this way. If untrusted clients reach your
services, remove the `muzzle` member from incoming `tracestate` at the edge.

## Examples

Example usage can be seen in the
[`example/trace_demonstration.rb` file](https://github.com/ericmustin/otel-muzzle/blob/main/ruby/example/trace_demonstration.rb).

## References

- [otel-muzzle design](https://github.com/ericmustin/otel-muzzle/blob/main/docs/design.md): the sampling rule, the
  ratio, the patch and the dependency choices
- [Contributing to otel-muzzle](https://github.com/ericmustin/otel-muzzle/blob/main/CONTRIBUTING.md)
- [OpenTelemetry Ruby](https://opentelemetry.io/docs/languages/ruby/)
- [OpenTelemetry Ruby instrumentation libraries](https://opentelemetry.io/docs/languages/ruby/libraries/)

## License

The `otel-muzzle` gem is distributed under the Apache 2.0 license. See
[LICENSE](https://github.com/ericmustin/otel-muzzle/blob/main/ruby/LICENSE) for more information.
