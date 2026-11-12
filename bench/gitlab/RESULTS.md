# otel-muzzle on GitLab

Run `20261110T042816Z`: GitLab CE 19.4.5-ce.0, k6 at 4 iterations/s for 600s per variant, after a warmup that is not counted. k6 requests: baseline 2,465, muzzle-0 2,465, muzzle-10 2,465, header-10 2,465, baseline-2 2,464.

## Spans and bytes per request

Everything GitLab exported during the measured window (web requests, Sidekiq jobs, cron and polling), divided by the k6 requests in it, and the change against GitLab's own tracing. OTLP bytes are the export requests as the collector received them, uncompressed.

| Variant | Spans per request | OTLP bytes per request |
|---|---:|---:|
| GitLab's own tracing (baseline) | 180.96 (+0.0%) | 51.59 KB (+0.0%) |
| otel-muzzle, ratio 0% | 104.91 (-42.0%) | 41.12 KB (-20.3%) |
| otel-muzzle, ratio 10% (default) | 112.82 (-37.7%) | 41.89 KB (-18.8%) |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 111.37 (-38.5%) | 42.30 KB (-18.0%) |
| GitLab's own tracing, second run | 181.37 (+0.2%) | 51.68 KB (+0.2%) |

## By process

Spans per k6 request from the web server (Puma) and the background workers (Sidekiq).

| Variant | Web | Sidekiq |
|---|---:|---:|
| GitLab's own tracing (baseline) | 128.48 (+0.0%) | 52.49 (+0.0%) |
| otel-muzzle, ratio 0% | 62.31 (-51.5%) | 42.60 (-18.8%) |
| otel-muzzle, ratio 10% (default) | 69.44 (-45.9%) | 43.37 (-17.4%) |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 68.27 (-46.9%) | 43.10 (-17.9%) |
| GitLab's own tracing, second run | 128.51 (+0.0%) | 52.86 (+0.7%) |

## By kind

Spans per k6 request by span kind. otel-muzzle drops only INTERNAL spans.

| Variant | Internal | Server | Client | Producer | Consumer |
|---|---:|---:|---:|---:|---:|
| GitLab's own tracing (baseline) | 76.53 (+0.0%) | 1.13 (+0.0%) | 101.73 (+0.0%) | 0.79 (+0.0%) | 0.79 (+0.0%) |
| otel-muzzle, ratio 0% | 0.00 (-100.0%) | 1.13 (+0.0%) | 102.12 (+0.4%) | 0.83 (+5.0%) | 0.83 (+6.0%) |
| otel-muzzle, ratio 10% (default) | 8.90 (-88.4%) | 1.13 (-0.0%) | 101.18 (-0.5%) | 0.81 (+3.3%) | 0.79 (-0.1%) |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 5.95 (-92.2%) | 1.13 (+0.0%) | 102.59 (+0.9%) | 0.85 (+7.7%) | 0.85 (+7.8%) |
| GitLab's own tracing, second run | 76.53 (+0.0%) | 1.13 (+0.0%) | 102.09 (+0.4%) | 0.80 (+2.2%) | 0.81 (+2.4%) |

## Request traces by type

Traces started by a k6 request, by their root span, with otel-muzzle, ratio 10% (default) against GitLab's own tracing (baseline). Trace bytes are the encoded spans of the trace, without the resource and scope each export repeats.

| Request | Traces | Spans per trace, stock | with otel-muzzle | Trace bytes, stock | with otel-muzzle |
|---|---:|---:|---:|---:|---:|
| `ProjectsController#show` | 324 | 187.19 | 102.59 (-45.2%) | 53.08 KB | 40.36 KB (-24.0%) |
| `Projects::WorkItemsController#index` | 205 | 112.94 | 57.09 (-49.5%) | 30.82 KB | 21.96 KB (-28.8%) |
| `Projects::BlobController#show` | 201 | 133.60 | 70.72 (-47.1%) | 38.34 KB | 28.57 KB (-25.5%) |
| `Projects::CommitsController#show` | 186 | 136.09 | 71.77 (-47.3%) | 39.24 KB | 29.78 KB (-24.1%) |
| `Projects::WorkItemsController#show` | 177 | 120.75 | 59.57 (-50.7%) | 35.12 KB | 25.70 KB (-26.8%) |
| `Projects::TreeController#show` | 176 | 129.12 | 67.74 (-47.5%) | 36.32 KB | 26.66 KB (-26.6%) |
| `HTTP GET /api/v4/projects/:id/issues` | 134 | 74.63 | 42.79 (-42.7%) | 28.78 KB | 26.03 KB (-9.5%) |
| `Projects::CommitController#show` | 134 | 138.94 | 68.80 (-50.5%) | 37.87 KB | 26.95 KB (-28.8%) |
| `Projects::MergeRequestsController#show` | 132 | 174.43 | 81.56 (-53.2%) | 49.76 KB | 35.07 KB (-29.5%) |
| `Projects::MergeRequestsController#index` | 119 | 115.62 | 57.46 (-50.3%) | 31.43 KB | 22.06 KB (-29.8%) |
| `GraphqlController#execute` | 102 | 60.37 | 30.09 (-50.2%) | 20.57 KB | 17.68 KB (-14.0%) |
| `Projects::MergeRequestsController#diffs` | 95 | 183.38 | 84.09 (-54.1%) | 50.82 KB | 35.38 KB (-30.4%) |
| other | 551 | 76.74 | 51.07 (-33.4%) | 26.52 KB | 24.25 KB (-8.5%) |
| **all requests** | 2,536 | 122.76 | 65.75 (-46.4%) | 36.24 KB | 27.99 KB (-22.8%) |

## Background traces

Traces no k6 request started directly: Sidekiq jobs (each its own trace, linked to the request that queued it), cron jobs, polling, and GitLab's internal API calls from Gitaly.

| Variant | Traces | Spans per trace |
|---|---:|---:|
| GitLab's own tracing (baseline) | 38,704 | 3.48 (+0.0%) |
| otel-muzzle, ratio 0% | 37,215 | 2.94 (-15.7%) |
| otel-muzzle, ratio 10% (default) | 37,465 | 2.97 (-14.6%) |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 37,626 | 2.94 (-15.6%) |
| GitLab's own tracing, second run | 38,861 | 3.49 (+0.3%) |

## Checks

- *Cut* traces have no root in the window, because it opened or closed while they ran; they are left out of the other checks.
- *Dangling* spans point at a parent that was never exported. Must be 0.
- *Rule violations* are INTERNAL spans under a local parent in a trace that is not verbose. Must be 0.
- *Spread* compares SERVER, CLIENT, PRODUCER and CONSUMER spans per request trace, by request type, and the INTERNAL spans of verbose traces, relative to their request type's stock mean, with the two stock runs. A failure is a mean further from stock than the stock runs are from each other, or than three standard errors, whichever is larger. The second stock run must also be within 3% of the first on spans and on OTLP bytes per request.
- *Outbound calls* are HTTP calls to anything outside the Compose network, in the warmup or the measured window. Must be 0.
- *k6 checks* must all pass.

| Variant | Request traces | Background traces | Cut | Dangling | Rule violations | Spread failures | Outbound calls | k6 checks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GitLab's own tracing (baseline) | 2,536 | 38,704 | 0 | 0 | n/a | n/a | 0 | 100.0% |
| otel-muzzle, ratio 0% | 2,536 | 37,215 | 0 | 0 | 0 | 0 | 0 | 100.0% |
| otel-muzzle, ratio 10% (default) | 2,536 | 37,465 | 0 | 0 | 0 | 0 | 0 | 100.0% |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 2,521 | 37,626 | 0 | 0 | 0 | 0 | 0 | 100.0% |
| GitLab's own tracing, second run | 2,535 | 38,861 | 0 | 0 | n/a | 0 | 0 | 100.0% |

Every check passed.
