# otel-muzzle benchmark on OpenTelemetry Demo services

This benchmark measures how much trace data otel-muzzle removes from real, unmodified services.

- **The services:** the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) 3.1.0 images for
  Ruby, Node.js and Python, plus a Go control.
- **The load:** the same k6 load each time, once with the stock images and once per otel-muzzle setting.
- **Per service and variant it reports:**
  - spans per trace;
  - trace size, as the encoded bytes of the spans in each trace;
  - the OTLP bytes the SDK sends;
  - the change against stock;
  - a GB/day projection;
  - load-test latency.
- **Integrity checks:**
  - every request still has its server span;
  - no exported span points at a missing parent;
  - every trace kept its INTERNAL spans exactly when otel-muzzle's rule says it should.

Everything runs in local Docker. Nothing is pushed to a registry or published anywhere. If `../.env` sets
`OTLP_FORWARD_ENDPOINT` and `OTLP_FORWARD_AUTHORIZATION`, the collector also forwards the spans to that OTLP
endpoint (see [Forwarding](#forwarding)). `LOCAL_ONLY=1` turns that off.

The latest results are in [RESULTS.md](RESULTS.md), and every scheduled run is on the
[dashboard](https://ericmustin.github.io/otel-muzzle/) (see [Results over time](#results-over-time)). The same
comparison on a full application, GitLab CE with its own OpenTelemetry tracing, is in [gitlab/](gitlab/README.md).

## Run it

Requirements: Docker with Compose v2, `bash`, and `make`. The first run pulls about 2 GB of images and builds three
small derived images.

```
make bench          # full matrix: 7 variants, 25 req/s per service for 40 s each (about 15 minutes)
make bench-quick    # 5 variants, 10 req/s for 15 s each (about 5 minutes)
make bench-down     # remove anything left running

LOCAL_ONLY=1 make bench                   # never forward spans
RATE=50 DURATION=60s PROJECT_RPS=500 make bench
VARIANTS="baseline muzzle-0 muzzle-10" make bench
```

Each run writes `bench/out/<run-id>/` (git-ignored).
- It contains `results.md`, `results.csv` and `results.json`.
- For each variant it also keeps the raw collector output, the k6 summaries and the container logs.
- `RESULTS.md` is overwritten with the latest report.
- The run fails, with the report still written, when an integrity check fails.

## What runs

| Service | Language | Image | Called with |
|---|---|---|---|
| email | Ruby (Sinatra) | `ghcr.io/open-telemetry/demo:3.1.0-email` | HTTP `POST /send_order_confirmation` |
| payment | Node.js (gRPC) | `ghcr.io/open-telemetry/demo:3.1.0-payment` | gRPC `PaymentService/Charge` |
| recommendation | Python (gRPC) | `ghcr.io/open-telemetry/demo:3.1.0-recommendation` | gRPC `RecommendationService/ListRecommendations` |
| product-catalog | Go | `ghcr.io/open-telemetry/demo:3.1.0-product-catalog` | by recommendation; the control, it never gets the package |

The stack also includes flagd (with every demo flag `off`), the product catalog's Postgres (`astronomy-db`), an OpenTelemetry Collector, and k6. Demo files the services need are vendored under `vendor/opentelemetry-demo-3.1.0/`. No host ports are published.

What the stock services emit per request:

| Service | Spans per request | Of which INTERNAL |
|---|---|---|
| email | Rack server span, `send_email`, two `sinatra.render_template` | 3 |
| payment | gRPC server span, `charge` | 1 |
| recommendation | gRPC server span, `get_product_list`, gRPC client call to product-catalog | 1 |

## Variants

| Name | Images | `OTEL_MUZZLE_VERBOSE_RATIO` | Requests sending `muzzle=verbose` |
|---|---|---:|---:|
| `baseline` | stock demo images | | |
| `muzzle-0` | stock + otel-muzzle | 0 | |
| `muzzle-10` | stock + otel-muzzle | 0.1 (the default) | |
| `muzzle-25`, `-50`, `-100` | stock + otel-muzzle | 0.25 / 0.5 / 1 | |
| `header-10` | stock + otel-muzzle | 0 | 10% (with a sampled `traceparent`) |

A ratio `r` keeps every span of a share `r` of traces, picked by trace id. The header variant shows the on-demand
path: a caller forces one request's trace to full detail.

## How otel-muzzle is added to each stock image

Only the three service images change, and each change is that language's documented install path for an app
set up the way the demo's is:

- **Ruby, `c.use`** (`images/email/`). The demo calls
  `OpenTelemetry::SDK.configure { |c| c.use "OpenTelemetry::Instrumentation::Sinatra" }` and never calls
  `Bundler.require`.
  - The gem joins the app's bundle. It has no dependencies, so the bundle's gems are unchanged.
  - `patch.rb` adds `require "otel/muzzle"` and `c.use "OpenTelemetry::Instrumentation::Muzzle"` next to the app's
    own lines, and fails the build if those lines ever move.
- **Node.js, setup file** (`images/payment/`). The demo starts
  `@opentelemetry/auto-instrumentations-node`'s `register` (0.78.0) through `NODE_OPTIONS`.
  - The derived image swaps in `--require` of the standard setup file (`examples/js-app/instrumentation.js`),
    installed next to the same versions of
    `auto-instrumentations-node`, `sdk-node`, `sdk-trace` and `api`.
  - It loads the same instrumentations and adds otel-muzzle. Nothing else changes.
- **Python, `opentelemetry-instrument`** (`images/recommendation/`). The demo already runs under
  `opentelemetry-instrument`.
  - The package is pip-installed into the app's venv with `--no-deps`, so the pinned OpenTelemetry stays as it
    is.
  - Its `muzzle` entry point loads by itself.

## How it is measured

1. **Warmup and measurement are separate files.**
   - For each variant, the three services are recreated and warmed up.
   - The runner then waits past the SDKs' 5 s batch delay and restarts the collector on a new output file for the
     measured phase.
   - One-time spans, such as the first flagd connection, DNS lookups and gRPC channel setup, stay out of the numbers.
2. **The same load every time.** k6 holds a constant arrival rate per service, so every variant gets essentially
   the same number of requests. Each request is one trace in each service it reaches.
3. **Trace size is span bytes.**
   - The collector writes traces with the `file` exporter, in `format: proto` and with no batching.
   - The analyzer (`analyze.py`, standard library only) parses that protobuf directly.
   - It sums the encoded size of each span by service and trace. That is the trace's size without the resource
     and scope every export repeats.
   - "OTLP bytes" are the whole export requests, resource and scope included: what the SDK actually sends.
   - Metrics and logs from the demo services are dropped and not counted.
4. **Integrity checks.**
   - In every variant, each service must export exactly one server span per request. Otherwise spans were lost
     rather than dropped.
   - No non-server span may reference a parent that was never exported, which would mean a broken trace.
   - In every otel-muzzle variant, each trace must keep its INTERNAL spans exactly when the variant's ratio selects
     its trace id or its tracestate carries `muzzle=verbose`. That is the same rule as
     `testdata/ratio_vectors.json`, checked on every trace under load.
   - The Go control service must stay flat.

## Reading RESULTS.md

- **Spans per trace, trace size, OTLP bytes per request.** Variants by language, with absolute values and the
  change against stock.
- **Per-service detail.**
  - Spans by kind.
  - Trace bytes, mean and p95.
  - The share of traces that came out verbose (about 10% in `muzzle-10`).
  - Rule mismatches, which must be 0.
- **Verbose ratio: modeled versus measured.** With the ratio at `r`, the expected span reduction is `(1 - r)` times
  the ratio-0 reduction. The measured rows should track it within sampling noise.
- **What a verbose trace costs.** Bytes per span at ratio 100% against stock. otel-muzzle adds no attributes and no
  tracestate, so they should match within noise.
- **Projection.** Measured bytes and spans per request, extrapolated linearly to `PROJECT_RPS` requests per second
  per service (default 100), per day.
- **Load test and trace integrity.** k6 success rate and latency, the server-span check, dangling parents and rule
  mismatches.

Byte reductions differ from span reductions. The server span, which every variant keeps, carries most of the
attributes. So dropping a small INTERNAL span saves less than its share of the span count, and dropping an
attribute-heavy one saves more.

## Results over time

The `bench` workflow (`.github/workflows/bench.yml`) runs both benchmarks on GitHub-hosted runners:

| Benchmark | Schedule (UTC) | Variants |
|---|---|---|
| OpenTelemetry Demo | nightly, 03:17 | `baseline`, `muzzle-0`, `muzzle-10`, `muzzle-100`, `header-10` |
| GitLab CE | Sundays, 05:17 | `baseline`, `muzzle-0`, `muzzle-10`, `header-10`, `baseline-2` |

Both also run on pull requests that change `bench/`. `gh workflow run bench` starts the OpenTelemetry Demo
benchmark by hand, and `gh workflow run bench -f gitlab=true` the GitLab one.

Each run:

1. **Reports.** It writes `results.md` to the job summary and uploads the run directory, without the raw collector
   output, as an artifact.
2. **Converts.** `history.py` turns `results.json` into the input of
   [github-action-benchmark](https://github.com/benchmark-action/github-action-benchmark), the action
   OpenTelemetry's JavaScript and Python repositories use for their benchmark history. Each entry is one
   measure of one variant, such as `email/muzzle-10/trace_bytes`. Every measure is spans or bytes per request or per
   trace, so smaller is better. `history.py` refuses a run whose checks failed, so the history holds only runs that
   passed them.
3. **Records.** On `main` only, the action appends the numbers and the commit to `benchmarks/data.js` on the
   `benchmark-data` branch. When a number for a new commit is more than 20% above the previous commit's, it
   comments on the commit. Runs on the same commit add points to the history but are not compared with each other.

The `pages` workflow then publishes `site/index.html` with that `data.js` to GitHub Pages as the dashboard:

- one chart per service and measure over time, with a line per variant;
- the change against stock, computed per run;
- the latest run as a table.

A link can select the view, as `?<measure>&change#<benchmark>` does in `?trace_bytes&change#email`.

The measures are counts and encoded sizes, so the runner's speed does not move them. They change when otel-muzzle,
the pinned images or the load change. The ratio variants also vary by about 2% between runs, because the verbose
traces are picked at random by trace id.

To set this up in a fork, create the data branch and switch GitHub Pages to deploy from Actions, once:

```sh
git switch --orphan benchmark-data && git commit --allow-empty -m "Start the benchmark data" \
  && git push origin benchmark-data && git switch -
gh api -X POST 'repos/{owner}/{repo}/pages' -f build_type=workflow
```

## Forwarding

`run.sh` only forwards spans when `../.env` defines `OTLP_FORWARD_ENDPOINT` and `OTLP_FORWARD_AUTHORIZATION`, the
full value of the `Authorization` header, such as `ApiKey <key>` or `Bearer <token>`.

1. **Preflight.** It first sends one empty OTLP request with those credentials. If the endpoint answers with
   anything other than 2xx, the run prints a warning and stays local.
2. **Forwarding.** Otherwise `run.sh` merges `collector/forward.yaml` into the collector config.
   - A second traces pipeline forwards the same spans to that OTLP/HTTP endpoint.
   - It sets `deployment.environment.name=muzzle-bench-<variant>`, so each variant is its own environment in
     the backend.
3. **Viewing.** In your backend, filter by service (`email`, `payment`, `recommendation`) and switch between the
   `muzzle-bench-*` environments.

The local numbers never depend on this path. `run.sh` passes the credentials only to the preflight request and the
collector container, and never writes them to disk or logs. The report header states whether spans were forwarded.

## Caveats

- **These demo services are small**, with one to four spans per request. Real services with ORM, middleware,
  HTTP-client and cache instrumentation carry more INTERNAL spans. The repository's `examples/` apps show that
  shape.
- **The Go control service's bytes grow slightly in `header-10`.** The `muzzle=verbose` tracestate member
  propagates to it on the recommendation service's outgoing calls. Ratio variants add nothing to the wire.
- **Ratio variants are random by trace id.** With about 1,000 requests per service, the verbose share varies by a
  couple of percentage points between runs.
- **Latency comes from one laptop's Docker network.** Read it as "no errors, no obvious regression", not as a
  performance measurement.
