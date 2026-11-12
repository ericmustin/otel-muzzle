# The example stack and the end-to-end test

`docker-compose.yml` at the repository root runs three small apps, one per language, each with otel-muzzle. A
request to the Ruby app travels through all three, and each app writes to Postgres. The verifier sends four such
requests and checks every span of the four traces.

## What runs

```
                 /chain              /chain                /hello
curl ──▶ ruby-app ──────▶ js-app ─────────▶ python-app ─────────▶ js-app
         :4567            :3000             :5001 (5000 inside)
            │                │                  │                    │
            └────────────────┴──────── Postgres ┴────────────────────┘

every app ──OTLP──▶ otel-collector ──▶ examples/out/spans.jsonl   (what the verifier reads)
                                   └─▶ Jaeger, http://localhost:16686
```

Each app handles a request the same way:

```
SERVER  GET /chain
└─ INTERNAL  ruby.work / js.work / py.work      dropped by otel-muzzle unless the trace is verbose
   ├─ CLIENT  the Postgres query                 kept; moves up to the SERVER span when work is dropped
   └─ CLIENT  the call to the next app           kept; moves up to the SERVER span when work is dropped
```

A full trace has 21 spans. With otel-muzzle it has 14: the INTERNAL spans are gone (the Node.js app also loses
Express's `request handler` span, and the Ruby app a `connect` span), and every CLIENT span hangs off its own
service's SERVER span.

| Service | Port | |
|---|---|---|
| `ruby-app` | 4567 | Sinatra, the entry point |
| `js-app` | 3000 | Express |
| `python-app` | 5001 | Flask (5001 on the host, because macOS uses 5000) |
| `postgres` | | |
| `otel-collector` | 4318 | OTLP over HTTP |
| `jaeger` | 16686 | the trace UI |
| `verifier` | | the test; only with `docker compose run` |

## Run it

From the repository root:

```sh
docker compose up --build -d                   # the stack, without the test
curl localhost:4567/chain                      # one request; open http://localhost:16686 to see its trace
curl -H 'traceparent: 00-4bf92f3577b34da6a3ce929d8e0e4736-00f067aa0ba902b7-01' \
     -H 'tracestate: muzzle=verbose' localhost:4567/chain     # a trace the ratio skips, made verbose by the header

docker compose run --rm --build verifier       # the test: exits 0 when every scenario passes
docker compose down -v                         # remove everything
```

`docker compose run ... verifier` builds whatever changed, starts any part of the stack that isn't running, and
waits for every app to report healthy before it sends anything. When it exits, the stack stays up, so you can look
at the same traces in Jaeger. `docker compose down -v` removes it.

The Make targets wrap the same commands, then remove the stack:

| Command | What it does |
|---|---|
| `make e2e` | the test, in-code setup |
| `make e2e MODE=zerocode` | the test, zero-code setup |
| `make e2e MODE=operator` | the test, OpenTelemetry Operator setup |
| `make e2e-delegate [MODE=…]` | the test with `OTEL_TRACES_SAMPLER=parentbased_traceidratio` at 0 (see below) |
| `make e2e-ruby` | the Ruby app alone, `GET /hello` |
| `make e2e KEEP=1` | the test, then leaves the stack running for Jaeger; `make down` removes it |
| `make up` / `make down` | the stack without the test / remove it |

## The three setups

Each setup is a compose override file on top of `docker-compose.yml`. With plain compose, pass both files before
the subcommand, for example `docker compose -f docker-compose.yml -f examples/compose/operator.yml run --rm --build
verifier`, and pass the same two files to `down`.

| | Ruby | Node.js | Python |
|---|---|---|---|
| **code** (default) | `gem 'otel-muzzle'`, then `c.use_all` | `NodeSDK` with `sampler: new MuzzleSampler()`, then `installMuzzle()` | `otel_muzzle.install()` |
| **zerocode** (`examples/compose/zerocode.yml`) | the zero-code loader through `RUBYOPT` | no zero-code entry point: a setup file loaded with `node --require ./instrumentation.js` | `opentelemetry-instrument` |
| **operator** (`examples/compose/operator.yml`) | the Operator's Ruby image, copied in by an init container, loaded through `RUBYOPT` | a custom Operator image (`examples/operator/nodejs`) loaded through `NODE_OPTIONS` | the Operator's Python image on `PYTHONPATH`; otel-muzzle in the app image |

The operator setup reproduces what the OpenTelemetry Operator does to a pod, without Kubernetes. An init container
copies the language's auto-instrumentation image into a shared volume, and the app's environment points the
runtime at it, using the same variables the Operator sets.

## Example backends

[backends/elastic](backends/elastic/README.md) sends the same traces to Elastic Observability, with otel-muzzle off and on.

## What the test checks

`examples/verify.py` sends four requests to `ruby-app`'s `/chain` and finds each trace in
`examples/out/spans.jsonl`:

| Scenario | Request | Expected |
|---|---|---|
| 1 quiet trace id | a sampled `traceparent` whose trace id the ratio does not select | lean: 14 spans |
| 2 `tracestate: muzzle=verbose` | the same, plus the header | full: 21 spans, and every downstream app received the header |
| 3 trace id the ratio selects | a sampled `traceparent` whose last 8 hex characters fall within 10% | full: 21 spans, so all three languages chose the same traces |
| 4 no `traceparent` | nothing | lean or full, whichever the new trace id implies |

**Lean** means:
- there are no INTERNAL spans;
- every CLIENT span's parent is its own service's SERVER span. Each CLIENT span was started under a dropped
  INTERNAL span, so this is the patch at work.

**Full** means every app kept its INTERNAL span.

**Both** also require:
- one root;
- every parent exported, so the trace is connected;
- each downstream SERVER span under a CLIENT span of the app that called it;
- all three apps present.

The output prints each trace as a tree:

```
--- scenario 1 quiet trace id ---
trace 474b1af7167cbffa42e06178234b5119: 14 spans, expected lean
  ruby-app    SERVER   GET /chain  (parent = traceparent)
  ruby-app    CLIENT     connect
  ruby-app    CLIENT     SELECT app
  ruby-app    CLIENT     GET
  js-app      SERVER       GET /chain
  ...
PASS
```

It ends with a summary, then `ALL PASS` (exit 0) or `N of 4 scenarios FAILED` (exit 1).

`make e2e-delegate` checks that the sampler you configure keeps the final say. The apps run
`OTEL_TRACES_SAMPLER=parentbased_traceidratio` with `OTEL_TRACES_SAMPLER_ARG=0`, which drops every new trace.
Scenario 4 then expects no spans at all from any app. Scenarios 1–3 still arrive with a sampled `traceparent`, so
they still pass.

## How long it takes, and when it finishes

A run with the images already built takes about 20–30 seconds:

| Phase | Typical time |
|---|---|
| build (cached), start Postgres, the collector and Jaeger | 2–12 s |
| the apps start and report healthy | 2–4 s |
| scenarios 1–3 | about 2 s each |
| scenario 4 | about 2 s; 8 s in `e2e-delegate`, which waits out the SDKs' 5 s export delay to be sure nothing arrives |
| `down -v` | 3–4 s |

The first run also pulls Postgres, the collector, Jaeger and the base images, and builds the three app images. That
takes a few minutes.

**Every wait has a limit, so a broken run fails rather than hangs:**
- Compose gives each app 120 s (60 health checks, 2 s apart) to report healthy. Otherwise `run` stops with "dependency
  failed to start".
- Each request times out after 15 s.
- The verifier waits up to 20 s for a trace's spans. It stops early once every app's SERVER span is in and nothing
  new has arrived for 1.5 s.

So the worst case is about 5 minutes.

## When it fails

- `make e2e` prints the last 80 log lines of every service, then removes the stack.
- To dig further:
  - run `make e2e KEEP=1`, or plain `docker compose run --rm --build verifier`, which leaves the stack up;
  - look at the trace in Jaeger, `docker compose logs <service>` and `examples/out/spans.jsonl`;
  - finish with `docker compose down -v`.
- `python3 examples/verify.py --selftest` checks the verifier itself against hand-written spans, without Docker.
  `make test` runs it.
