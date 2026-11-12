# otel-muzzle

This is an experimental package under active development. New releases may include breaking changes.

otel-muzzle is an OpenTelemetry add-on for Ruby, Python and Node.js. It drops INTERNAL spans whose parent is a local
span, and keeps every trace one connected tree: the children of a dropped span attach to its nearest kept ancestor.
Everything else stays: SERVER, CLIENT, PRODUCER and CONSUMER spans (database calls among them), root spans, and
spans with a remote parent.

By default one trace in ten stays verbose and keeps every span. A request can ask for a verbose trace with a W3C
`tracestate: muzzle=verbose` header, and every service it reaches that runs otel-muzzle honours it.

```text
default                                     verbose trace
GET /orders        SERVER                   GET /orders        SERVER
└─ SELECT orders   CLIENT  parent: GET /    └─ load_orders     INTERNAL
                                               └─ SELECT orders   CLIENT
```

On the OpenTelemetry Demo's own services at the default setting, spans per trace fell by
<!-- bench:headline -->49.6% and trace size by 42.5%<!-- /bench:headline -->. See [the benchmark](#benchmark).

## Packages

| Language | Package | Setup | Docs |
|---|---|---|---|
| Ruby | `otel-muzzle` | `c.use_all` | [ruby/README.md](ruby/README.md) |
| Python | `otel-muzzle` | `opentelemetry-instrument` | [python/README.md](python/README.md) |
| Node.js | `otel-muzzle` | A `NodeSDK` setup file loaded with `node --require` | [js/README.md](js/README.md) |

The packages are not yet published to a registry; each package README shows how to install from this repository.
Each package works in code next to your own SDK setup, with the language's zero-code instrumentation, and under the
[OpenTelemetry Operator](https://github.com/open-telemetry/opentelemetry-operator). Node.js has no zero-code entry
point of its own: it uses the standard setup file. The package READMEs show each way.

## Configuration

The same in every language:

| Setting | Default | Effect |
|---|---|---|
| `OTEL_MUZZLE_VERBOSE_RATIO` | `0.1` | share of traces that keep every span, in `[0, 1]` |
| `tracestate: muzzle=verbose` | | sent with a valid, sampled `traceparent`, makes that trace verbose everywhere |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` | the sampler otel-muzzle wraps; its decisions stand |

Every service makes the same ratio choice from the trace id (bytes 12–15), so a trace is verbose in all three
languages or in none, with nothing extra on the wire. The choice does not depend on which traces head sampling keeps,
so 1 in 10 of the traces you keep is verbose.

Anyone who can send headers can force a verbose trace, and so raise span volume. Strip the `muzzle` member from
incoming `tracestate` at your edge if that matters.

## How it works

Two small pieces, the same in each language. [docs/design.md](docs/design.md) is the full contract.

1. A sampler wraps the one you already have. Your sampler decides first, and its DROP always wins. When it keeps a
   span, otel-muzzle drops it only if the span is INTERNAL, its parent is a local span, and the trace is not
   verbose.
2. A `start_span` patch keeps the trace connected. A sampler can't do this on its own: every SDK gives a dropped
   span a fresh span id, so its children would point at a parent that is never exported. When the SDK returns a
   non-recording span under a sampled local parent, the patch hands back the parent's context instead, and the
   children attach to the parent. The patch holds no policy of its own.

## Benchmark

`make bench` runs the stock [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) 3.1.0
services for each language under the same k6 load. It runs them once as shipped and once per otel-muzzle
setting, then reports spans per trace, trace size and OTLP bytes for each service. The services are:

- email (Ruby);
- payment (Node.js);
- recommendation (Python);
- product-catalog (Go), a control that never gets otel-muzzle.

It also checks every trace against the rule, and checks that no exported span points at a missing parent.

<!-- bench:table -->
At the default ratio (10%), about 1,000 requests per service, run `20261107T144854Z`:

| Service | Spans per trace, stock | with otel-muzzle | Trace size, stock | with otel-muzzle |
|---|---:|---:|---:|---:|
| email (Ruby) | 4.00 | 1.32 (−67.1%) | 712 B | 396 B (−44.3%) |
| payment (Node.js) | 2.00 | 1.10 (−44.8%) | 459 B | 266 B (−42.0%) |
| recommendation (Python) | 3.00 | 2.11 (−29.6%) | 1,171 B | 685 B (−41.5%) |
| All three | 9.00 | 4.53 (−49.6%) | 2,342 B | 1,348 B (−42.5%) |

In every variant, every trace matched the rule, no exported span pointed at a missing parent, and every request
kept its server span. Verbose traces cost the same bytes per span as stock. These demo services are small, with one
to four spans per request; services with ORM, middleware and HTTP-client instrumentation carry more INTERNAL spans.
<!-- /bench:table -->

The payment row ran `otel-muzzle/register`, the entry point that the setup file has since replaced.

Latest numbers: [bench/RESULTS.md](bench/RESULTS.md). Method and caveats: [bench/README.md](bench/README.md).

### Real applications

<!-- bench:apps -->
| Application | Language | Spans per request, stock | with otel-muzzle (10%) | OTLP bytes per request, stock | with otel-muzzle | Details |
|---|---|---:|---:|---:|---:|---|
| GitLab CE 19.4.5 | Ruby | 180.96 | 112.82 (−37.7%) | 51.59 KB | 41.89 KB (−18.8%) | [results](bench/gitlab/RESULTS.md), [method](bench/gitlab/README.md) |

Each row counts everything the application exported with its own OpenTelemetry tracing, including background jobs,
cron and polling, per load-test request: 4 a second for 600 s here. Background work runs at a steady rate and loses
fewer spans; request traces alone had 46.4% fewer spans. In every variant, no exported span pointed at a missing
parent, no trace that was not verbose kept an INTERNAL span under a local parent, request traces kept their SERVER,
CLIENT, PRODUCER and CONSUMER spans within the spread of two stock runs, and the application called nothing outside
the machine.
<!-- /bench:apps -->

## Examples

`docker-compose.yml` runs a Ruby, a Node.js and a Python app, each with otel-muzzle, that call each other and write
to Postgres. A collector feeds Jaeger. The end-to-end test is one compose command:

```sh
docker compose up --build -d                  # the stack; curl localhost:4567/chain, then http://localhost:16686
docker compose run --rm --build verifier      # four requests, every span of their traces checked; exits 0 or 1
docker compose down -v
```

[examples/README.md](examples/README.md) explains the apps, the three setups (in code, zero-code, Operator), the
four scenarios, and how long a run takes.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers the repository layout, the test commands for each package, license headers,
commit messages and the shared test vectors.

## License

Apache-2.0. See [LICENSE](LICENSE).
