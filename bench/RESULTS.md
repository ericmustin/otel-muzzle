# otel-muzzle benchmark results

Run `20261107T144854Z`: OpenTelemetry Demo 3.1.0 services, 25 requests/s per service for 40s per variant (after a 10s warmup that is not counted). Each request is one trace in each service it reaches.
Everything stayed on this machine.
The payment row ran `otel-muzzle/register`, the entry point that the setup file has since replaced.

## Spans per trace

Mean spans each service exports per trace, and the change against the stock demo images.

| Variant | email (Ruby) | payment (Node.js) | recommendation (Python) | All three |
|---|---:|---:|---:|---:|
| Stock SDKs (baseline) | 4.00 (+0.0%) | 2.00 (+0.0%) | 3.00 (+0.0%) | 9.00 (+0.0%) |
| otel-muzzle, ratio 0% | 1.00 (-75.0%) | 1.00 (-50.0%) | 2.00 (-33.3%) | 4.00 (-55.6%) |
| otel-muzzle, ratio 10% (default) | 1.32 (-67.1%) | 1.10 (-44.8%) | 2.11 (-29.6%) | 4.53 (-49.6%) |
| otel-muzzle, ratio 25% | 1.75 (-56.2%) | 1.22 (-38.8%) | 2.26 (-24.7%) | 5.24 (-41.8%) |
| otel-muzzle, ratio 50% | 2.51 (-37.3%) | 1.50 (-24.9%) | 2.50 (-16.8%) | 6.51 (-27.7%) |
| otel-muzzle, ratio 100% | 4.00 (+0.0%) | 2.00 (+0.0%) | 3.00 (+0.0%) | 9.00 (+0.0%) |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 1.29 (-67.7%) | 1.10 (-45.2%) | 2.10 (-30.1%) | 4.48 (-50.2%) |

## Trace size

Mean encoded bytes of the spans each service exports per trace (OTLP protobuf, uncompressed, without the resource and scope that every export repeats), and the change against stock.

| Variant | email (Ruby) | payment (Node.js) | recommendation (Python) | All three |
|---|---:|---:|---:|---:|
| Stock SDKs (baseline) | 712 B (+0.0%) | 459 B (+0.0%) | 1,171 B (+0.0%) | 2,342 B (+0.0%) |
| otel-muzzle, ratio 0% | 359 B (-49.6%) | 244 B (-46.8%) | 623 B (-46.8%) | 1,226 B (-47.7%) |
| otel-muzzle, ratio 10% (default) | 396 B (-44.3%) | 266 B (-42.0%) | 685 B (-41.5%) | 1,348 B (-42.5%) |
| otel-muzzle, ratio 25% | 448 B (-37.1%) | 292 B (-36.3%) | 765 B (-34.7%) | 1,505 B (-35.8%) |
| otel-muzzle, ratio 50% | 536 B (-24.7%) | 352 B (-23.3%) | 895 B (-23.6%) | 1,783 B (-23.9%) |
| otel-muzzle, ratio 100% | 712 B (+0.0%) | 459 B (+0.0%) | 1,171 B (+0.0%) | 2,342 B (+0.0%) |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 400 B (-43.8%) | 269 B (-41.5%) | 682 B (-41.8%) | 1,351 B (-42.3%) |

## OTLP bytes sent per request

What the SDKs actually send, including the resource and scope of each export, per request.

| Variant | email (Ruby) | payment (Node.js) | recommendation (Python) | All three |
|---|---:|---:|---:|---:|
| Stock SDKs (baseline) | 717 B (+0.0%) | 466 B (+0.0%) | 1,175 B (+0.0%) | 2,358 B (+0.0%) |
| otel-muzzle, ratio 0% | 363 B (-49.3%) | 251 B (-46.1%) | 627 B (-46.7%) | 1,241 B (-47.4%) |
| otel-muzzle, ratio 10% (default) | 401 B (-44.0%) | 274 B (-41.3%) | 689 B (-41.4%) | 1,364 B (-42.2%) |
| otel-muzzle, ratio 25% | 453 B (-36.9%) | 300 B (-35.7%) | 769 B (-34.6%) | 1,521 B (-35.5%) |
| otel-muzzle, ratio 50% | 541 B (-24.5%) | 359 B (-22.9%) | 899 B (-23.5%) | 1,800 B (-23.7%) |
| otel-muzzle, ratio 100% | 717 B (+0.0%) | 466 B (+0.0%) | 1,175 B (-0.0%) | 2,358 B (+0.0%) |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 405 B (-43.5%) | 276 B (-40.8%) | 685 B (-41.7%) | 1,367 B (-42.0%) |

## Per-service detail

*Verbose traces* kept their INTERNAL spans. *Rule mismatches* count traces where that disagrees with otel-muzzle's rule (the ratio selects the trace id, or the tracestate carries `muzzle=verbose`); it must be 0.

### email (Ruby)

| Variant | Traces | Spans/trace | Server | Client | Internal | Trace bytes (mean / p95) | OTLP bytes/req | Verbose traces | Rule mismatches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Stock SDKs (baseline) | 1,001 | 4.00 | 1,001 | 0 | 3,003 | 712 / 712 | 717 | 100.0% | n/a |
| otel-muzzle, ratio 0% | 1,001 | 1.00 | 1,001 | 0 | 0 | 359 / 359 | 363 | 0.0% | 0 |
| otel-muzzle, ratio 10% (default) | 1,001 | 1.32 | 1,001 | 0 | 318 | 396 / 712 | 401 | 10.6% | 0 |
| otel-muzzle, ratio 25% | 1,001 | 1.75 | 1,001 | 0 | 753 | 448 / 712 | 453 | 25.1% | 0 |
| otel-muzzle, ratio 50% | 1,001 | 2.51 | 1,001 | 0 | 1,509 | 536 / 712 | 541 | 50.2% | 0 |
| otel-muzzle, ratio 100% | 1,000 | 4.00 | 1,000 | 0 | 3,000 | 712 / 712 | 717 | 100.0% | 0 |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 1,000 | 1.29 | 1,000 | 0 | 291 | 400 / 786 | 405 | 9.7% | 0 |

### payment (Node.js)

| Variant | Traces | Spans/trace | Server | Client | Internal | Trace bytes (mean / p95) | OTLP bytes/req | Verbose traces | Rule mismatches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Stock SDKs (baseline) | 1,000 | 2.00 | 1,000 | 0 | 1,000 | 459 / 461 | 466 | 100.0% | n/a |
| otel-muzzle, ratio 0% | 1,001 | 1.00 | 1,001 | 0 | 0 | 244 / 244 | 251 | 0.0% | 0 |
| otel-muzzle, ratio 10% (default) | 1,001 | 1.10 | 1,001 | 0 | 104 | 266 / 459 | 274 | 10.4% | 0 |
| otel-muzzle, ratio 25% | 1,001 | 1.22 | 1,001 | 0 | 225 | 292 / 461 | 300 | 22.5% | 0 |
| otel-muzzle, ratio 50% | 1,000 | 1.50 | 1,000 | 0 | 503 | 352 / 461 | 359 | 50.3% | 0 |
| otel-muzzle, ratio 100% | 1,001 | 2.00 | 1,001 | 0 | 1,001 | 459 / 461 | 466 | 100.0% | 0 |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 1,001 | 1.10 | 1,001 | 0 | 97 | 269 / 499 | 276 | 9.7% | 0 |

### recommendation (Python)

| Variant | Traces | Spans/trace | Server | Client | Internal | Trace bytes (mean / p95) | OTLP bytes/req | Verbose traces | Rule mismatches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Stock SDKs (baseline) | 1,001 | 3.00 | 1,001 | 1,001 | 1,001 | 1,171 / 1,171 | 1,175 | 100.0% | n/a |
| otel-muzzle, ratio 0% | 1,001 | 2.00 | 1,001 | 1,001 | 0 | 623 / 623 | 627 | 0.0% | 0 |
| otel-muzzle, ratio 10% (default) | 1,000 | 2.11 | 1,000 | 1,000 | 113 | 685 / 1,171 | 689 | 11.3% | 0 |
| otel-muzzle, ratio 25% | 1,001 | 2.26 | 1,001 | 1,001 | 259 | 765 / 1,171 | 769 | 25.9% | 0 |
| otel-muzzle, ratio 50% | 1,000 | 2.50 | 1,000 | 1,000 | 496 | 895 / 1,171 | 899 | 49.6% | 0 |
| otel-muzzle, ratio 100% | 1,001 | 3.00 | 1,001 | 1,001 | 1,001 | 1,171 / 1,171 | 1,175 | 100.0% | 0 |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 1,000 | 2.10 | 1,000 | 1,000 | 97 | 682 / 1,227 | 685 | 9.7% | 0 |

### product-catalog (Go): control, never gets otel-muzzle

| Variant | Traces | Spans/trace | Server | Client | Internal | Trace bytes (mean / p95) | OTLP bytes/req | Verbose traces | Rule mismatches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Stock SDKs (baseline) | 1,001 | 2.00 | 1,001 | 1,001 | 0 | 658 / 658 | 661 | 0.0% | n/a |
| otel-muzzle, ratio 0% | 1,001 | 2.00 | 1,001 | 1,001 | 0 | 658 / 658 | 661 | 0.0% | n/a |
| otel-muzzle, ratio 10% (default) | 1,000 | 2.00 | 1,000 | 1,000 | 0 | 658 / 658 | 661 | 0.0% | n/a |
| otel-muzzle, ratio 25% | 1,001 | 2.00 | 1,001 | 1,001 | 0 | 658 / 658 | 661 | 0.0% | n/a |
| otel-muzzle, ratio 50% | 1,000 | 2.00 | 1,000 | 1,000 | 0 | 658 / 658 | 661 | 0.0% | n/a |
| otel-muzzle, ratio 100% | 1,001 | 2.00 | 1,001 | 1,001 | 0 | 658 / 658 | 661 | 0.0% | n/a |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 1,000 | 2.00 | 1,000 | 1,000 | 0 | 661 / 690 | 664 | 0.0% | n/a |

## Verbose ratio: modeled versus measured

A ratio `r` keeps every span of a share `r` of traces. If `s0` is spans per trace with the ratio at 0 and `sb` the stock value, the expected span reduction is `(1 - r) * (1 - s0/sb)`.

| Ratio | email modeled | email measured | payment modeled | payment measured | recommendation modeled | recommendation measured |
|---:|---:|---:|---:|---:|---:|---:|
| 0% | -75.0% | -75.0% | -50.0% | -50.0% | -33.3% | -33.3% |
| 10% | -67.5% | -67.1% | -45.0% | -44.8% | -30.0% | -29.6% |
| 25% | -56.2% | -56.2% | -37.5% | -38.8% | -25.0% | -24.7% |
| 50% | -37.5% | -37.3% | -25.0% | -24.9% | -16.7% | -16.8% |
| 100% | -0.0% | +0.0% | -0.0% | +0.0% | -0.0% | +0.0% |

## What a verbose trace costs

At ratio 100% every span is kept, as in stock. otel-muzzle adds no attributes and no tracestate, so bytes per span should match stock within noise (span names, ids and timings vary).

| Service | Stock bytes/span | Ratio 100% bytes/span | Difference |
|---|---:|---:|---:|
| email (Ruby) | 178.0 | 178.0 | +0.0 B (+0.0%) |
| payment (Node.js) | 229.5 | 229.5 | +0.0 B (+0.0%) |
| recommendation (Python) | 390.3 | 390.3 | +0.0 B (+0.0%) |

## Projection at 100 requests/s per service

Linear extrapolation of the measured OTLP bytes and spans per request, per service, per day.

| Variant | email GB/day | payment GB/day | recommendation GB/day | Total GB/day | Saved GB/day | Spans/day (M) |
|---|---:|---:|---:|---:|---:|---:|
| Stock SDKs (baseline) | 6.20 | 4.03 | 10.15 | 20.37 | 0.00 | 77.8 |
| otel-muzzle, ratio 0% | 3.14 | 2.17 | 5.41 | 10.72 | 9.65 | 34.6 |
| otel-muzzle, ratio 10% (default) | 3.47 | 2.36 | 5.95 | 11.78 | 8.59 | 39.2 |
| otel-muzzle, ratio 25% | 3.91 | 2.59 | 6.64 | 13.14 | 7.23 | 45.2 |
| otel-muzzle, ratio 50% | 4.68 | 3.11 | 7.76 | 15.55 | 4.83 | 56.2 |
| otel-muzzle, ratio 100% | 6.20 | 4.03 | 10.15 | 20.37 | -0.00 | 77.8 |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 3.50 | 2.38 | 5.92 | 11.81 | 8.57 | 38.7 |

## Load test and trace integrity

Latency is what k6 measured from inside the Docker network on this machine, so read it as a sanity check rather than a performance claim. Every request must produce exactly one exported server span per service, otherwise spans were lost rather than dropped. A dangling parent is a non-server span whose parent was never exported, which means a broken trace; reparenting keeps it at zero. Rule mismatches are summed over the three services.

| Variant | email ok / p50 / p95 | payment ok / p50 / p95 | recommendation ok / p50 / p95 | Server spans = requests | Dangling parents | Rule mismatches |
|---|---|---|---|---|---:|---:|
| Stock SDKs (baseline) | 100.0% / 2.5 ms / 3.5 ms | 100.0% / 1.2 ms / 1.8 ms | 100.0% / 2.5 ms / 3.9 ms | yes | 0 | n/a |
| otel-muzzle, ratio 0% | 100.0% / 2.6 ms / 4.0 ms | 100.0% / 1.2 ms / 2.3 ms | 100.0% / 3.0 ms / 4.9 ms | yes | 0 | 0 |
| otel-muzzle, ratio 10% (default) | 100.0% / 2.5 ms / 3.6 ms | 100.0% / 1.2 ms / 2.0 ms | 100.0% / 2.4 ms / 3.8 ms | yes | 0 | 0 |
| otel-muzzle, ratio 25% | 100.0% / 2.5 ms / 3.7 ms | 100.0% / 1.2 ms / 1.9 ms | 100.0% / 2.2 ms / 3.5 ms | yes | 0 | 0 |
| otel-muzzle, ratio 50% | 100.0% / 2.6 ms / 3.6 ms | 100.0% / 1.2 ms / 1.8 ms | 100.0% / 2.4 ms / 3.8 ms | yes | 0 | 0 |
| otel-muzzle, ratio 100% | 100.0% / 2.6 ms / 4.1 ms | 100.0% / 1.2 ms / 2.1 ms | 100.0% / 2.3 ms / 4.4 ms | yes | 0 | 0 |
| otel-muzzle, ratio 0%, 10% of requests force verbose | 100.0% / 2.5 ms / 3.5 ms | 100.0% / 1.2 ms / 2.0 ms | 100.0% / 2.3 ms / 3.9 ms | yes | 0 | 0 |

## Variants

| Variant | Images | OTEL_MUZZLE_VERBOSE_RATIO | Requests sending `muzzle=verbose` |
|---|---|---:|---:|
| Stock SDKs (baseline) | stock demo 3.1.0 |  | 0% |
| otel-muzzle, ratio 0% | demo 3.1.0 + otel-muzzle | 0 | 0% |
| otel-muzzle, ratio 10% (default) | demo 3.1.0 + otel-muzzle | 0.1 | 0% |
| otel-muzzle, ratio 25% | demo 3.1.0 + otel-muzzle | 0.25 | 0% |
| otel-muzzle, ratio 50% | demo 3.1.0 + otel-muzzle | 0.5 | 0% |
| otel-muzzle, ratio 100% | demo 3.1.0 + otel-muzzle | 1 | 0% |
| otel-muzzle, ratio 0%, 10% of requests force verbose | demo 3.1.0 + otel-muzzle | 0 | 10% |
