# otel-muzzle benchmark on GitLab CE

This benchmark measures how much trace data otel-muzzle removes from GitLab, a large Rails application that ships
its own OpenTelemetry tracing. It runs the same team workload against GitLab as shipped and with otel-muzzle
added, and reports spans and OTLP bytes per request.

The latest results are in [RESULTS.md](RESULTS.md), and the weekly runs are on the
[dashboard](https://ericmustin.github.io/otel-muzzle/#gitlab). Everything runs in local Docker; no span leaves the
machine.

## Run it

Requirements: Docker with Compose v2, about 5 GB of free Docker memory, `bash`, and `make`. The first run pulls
the GitLab image (about 5.5 GB on disk) and builds two derived images.

```
make bench-gitlab          # 5 variants, 4 iterations/s for 600 s each (about 70 minutes)
make bench-gitlab-down     # stop it and remove GitLab's data volumes

RATE=6 DURATION=300s make bench-gitlab
VARIANTS="baseline muzzle-10 baseline-2" SKIP_BUILD=1 make bench-gitlab
```

Each run writes `bench/out/gitlab/<run-id>/` (git-ignored).
- It contains `run.json`, `results.md` and `results.json`.
- For each variant it also keeps the raw collector output, the k6 summaries, and the collector's and GitLab's
  logs.
- `RESULTS.md` is overwritten with the latest report.

The runner keeps GitLab's volumes between runs, so later runs skip the first boot and the seeding. The data grows
across runs, because the workload's writes accumulate, until `make bench-gitlab-down` removes the volumes.

## What runs

- **GitLab CE 19.4.5** (`gitlab/gitlab-ce:19.4.5-ce.0`), the Omnibus image.
  - Puma has 2 workers and Sidekiq a concurrency of 10, and the bundled Prometheus and KAS are off, so it fits
    in about 4 GB.
  - Cron jobs, Sidekiq's polling and the health checks run as shipped.
  - Auto DevOps is off ([`setup.rb`](setup.rb)). It is on by default, and with no runner every push adds a
    pipeline whose builds wait until an hourly job fails them all at once. In one run that burst added about
    410,000 spans to a window that otherwise held about 510,000.
  - Product usage events, Service Ping and the version check are off, and so is k6's usage report. Otherwise
    Sidekiq posts about 1,100 events a window to `events.gitlab.net`, and the background work would depend on
    reaching it.
- **An OpenTelemetry Collector**, which writes every export request to a file, as in [the demo
  benchmark](../README.md#how-it-is-measured).
- **k6**, with the workload below.

### GitLab's own tracing, and what changes

GitLab's Gemfile has an `:opentelemetry` group: the SDK, the OTLP exporter and 22 instrumentations (Rails,
ActionView, ActiveRecord, PG, Redis, Sidekiq, Grape, GraphQL and more). Omnibus installs it.
`config/initializers/opentelemetry.rb` loads it and calls `OpenTelemetry::SDK.configure(&:use_all)`, but only in
development, test, or on GitLab.com's staging URL.

| Image | Change |
|---|---|
| `muzzle-bench/gitlab:stock` | The initializer without its environment check, so `GITLAB_ENABLE_OTEL_EXPORTERS=true` alone turns tracing on ([`opentelemetry.rb`](opentelemetry.rb)) |
| `muzzle-bench/gitlab:muzzle` | The same, plus `gem 'otel-muzzle', path: …, group: :opentelemetry` in the Gemfile and a `bundle lock` |

`use_all` picks otel-muzzle up from GitLab's own group.
- **The `path:` option:** the gem is not on rubygems.org yet. A path gem also needs no entry in the lockfile's
  `CHECKSUMS` section, which GitLab's frozen bundle checks at boot.
- **The lockfile diff:** only otel-muzzle's own entries.

### Data and workload

- **Seed data:** the project `root/sinatra` holds [Sinatra](https://github.com/sinatra/sinatra) v4.2.1 and its
  history ([`seed.js`](seed.js) adds the rest). It has:
  - 40 issues with labels and comments;
  - 6 open merge requests.
- **The workload** ([`k6.js`](k6.js)): k6 runs a constant 4 iterations a second, each one weighted pick from the
  table below. It signs in once and shares the session, because GitLab allows 10 sign-ins a minute per IP address.
  - The workload signs in as the administrator, the `root` user.
  - Each iteration seeds its random choices with its own number, so every variant makes the same requests in the
    same order. Otherwise the number of writes, each of which starts about a dozen Sidekiq jobs, would vary
    between variants by more than otel-muzzle's effect on Sidekiq.

| Weight | Request |
|---:|---|
| 13 | project page |
| 8 each | file tree, a file, the commit list, the issue list, an issue |
| 6 | a commit |
| 5 each | the merge request list, a merge request |
| 4 | a merge request's diff |
| 6, 4, 3 | REST: issues, commits, the file tree |
| 4 | GraphQL: open issues with authors, assignees and labels |
| 4 each | REST writes: a new issue, a comment |
| 3 | REST writes: a commit on a new branch, then a merge request from it |

The writes start Sidekiq jobs, as they would for a team.

There are more request traces than k6 requests, because Workhorse sends an `/authorize` preflight to Rails for some
requests, and that preflight is a request trace of its own.

## Variants

| Name | Image | `OTEL_MUZZLE_VERBOSE_RATIO` | Requests sending `muzzle=verbose` |
|---|---|---:|---:|
| `baseline` | stock | | |
| `muzzle-0` | muzzle | 0 | |
| `muzzle-10` | muzzle | 0.1 (the default) | |
| `header-10` | muzzle | 0 | 10% (with a sampled `traceparent`) |
| `baseline-2` | stock | | |

The second stock run measures how much two identical runs differ. The checks use that spread as their tolerance.

## How it is measured

1. **Warmup and measurement are separate files.**
   - For each variant GitLab is recreated with its image and warmed up for 60 s.
   - The collector is then restarted on a new file for the measured phase.
   - GitLab keeps running throughout, because its background work belongs in the window too.
2. **The headline counts everything GitLab exported in the window, per k6 request.**
   - That includes Sidekiq jobs, cron jobs, Redis polling and GitLab's internal API calls from Gitaly, which is
     what an APM bill sees.
   - That background work runs at a steady rate, so the headline depends on the request rate.
   - Cron jobs and sidekiq-cron's poller run on timers, so a window holds one run more or fewer of each. One more
     run of the per-minute jobs is about 2,400 spans as shipped, and one more poll about 1,700. Over 600 s that
     moves the headline by less than 1%, and the second stock run shows how much it moved.
3. **Traces are sorted** by the analyzer ([`../analyze_app.py`](../analyze_app.py)) into three kinds:
   - *request* traces, whose root SERVER span answered k6 (its `User-Agent` is `muzzle-bench-k6`). These are
     also shown by request type: `Controller#action`, or the route for the API;
   - *background* traces;
   - *cut* traces, which have no root in the window because it opened or closed while they ran.
4. **Integrity checks**, which must all pass before a number counts:
   - no span in a request or background trace points at a parent that was never exported;
   - no non-verbose trace kept an INTERNAL span under a parent in the same process;
   - **spread:** SERVER, CLIENT, PRODUCER and CONSUMER spans per request trace, by request type, stay within the
     stock runs' spread. So do the INTERNAL spans of verbose traces, each relative to its request type's stock mean,
     so that a different mix of requests cannot pass for a change.
     - A failure is a mean further from stock than the two stock runs are from each other, or than three standard
       errors, whichever is larger;
   - the two stock runs are within 3% of each other on spans and on OTLP bytes per request;
   - GitLab makes no HTTP call outside the Compose network, in the warmup or the measured window;
   - every k6 check passes.

## Findings

- **Background jobs do not inherit a forced-verbose request.**
  - GitLab's Sidekiq and ActiveJob instrumentations use `propagation_style: :link`. A job is its own trace, linked
    to the request that queued it, rather than a child of it.
  - So `tracestate: muzzle=verbose` on a request does not reach the jobs it starts. Those jobs are verbose only
    when the ratio picks their own trace id.
- **Most background traces are a single Redis command, and otel-muzzle keeps them.**
  - Sidekiq's Redis commands outside a job, such as its polling, are each a one-span root trace. The Redis
    instrumentation's `trace_root_spans` option is on by default, and GitLab leaves it on.
  - In the stock run they were 94% of background traces and 27% of background spans. otel-muzzle never drops a
    CLIENT span, so they are 13% of what GitLab exports with otel-muzzle.
  - Sidekiq jobs carry most of the other background spans. otel-muzzle took them from 47.6 to 36.7 spans per
    job at the default ratio.
