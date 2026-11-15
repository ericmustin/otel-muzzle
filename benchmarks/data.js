window.BENCHMARK_DATA = {
  "lastUpdate": 1794752034458,
  "repoUrl": "https://github.com/ericmustin/otel-muzzle",
  "entries": {
    "OpenTelemetry Demo": [
      {
        "commit": {
          "author": {
            "name": "Eric Mustin",
            "username": "ericmustin",
            "email": "14250318+ericmustin@users.noreply.github.com"
          },
          "committer": {
            "name": "Eric Mustin",
            "username": "ericmustin",
            "email": "mustin.eric@gmail.com"
          },
          "id": "05389b665ae6eccef4d7ccc2cff10e7cdff30166",
          "message": "bench: restore the checkout before recording results\n\nThe first run on main failed at the github-action-benchmark step: run.sh\nrewrites the tracked bench/RESULTS.md, and the action switches the same\ncheckout to the benchmark-data branch, which git refuses with a modified file.\nThe workflow now restores the checkout first.\n\nPull requests skipped that step, which is how this got through. They now run\nit too, without pushing or commenting on alerts.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_016dSBta48mzNj6za2HiNrRu",
          "timestamp": "2026-11-15T13:31:29Z",
          "url": "https://github.com/ericmustin/otel-muzzle/commit/05389b665ae6eccef4d7ccc2cff10e7cdff30166"
        },
        "date": 1794752034202,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "email/baseline/spans_per_trace",
            "value": 4,
            "unit": "spans"
          },
          {
            "name": "email/baseline/trace_bytes",
            "value": 712,
            "unit": "bytes"
          },
          {
            "name": "email/baseline/otlp_bytes_per_request",
            "value": 717,
            "unit": "bytes"
          },
          {
            "name": "payment/baseline/spans_per_trace",
            "value": 2,
            "unit": "spans"
          },
          {
            "name": "payment/baseline/trace_bytes",
            "value": 459,
            "unit": "bytes"
          },
          {
            "name": "payment/baseline/otlp_bytes_per_request",
            "value": 468.1,
            "unit": "bytes"
          },
          {
            "name": "recommendation/baseline/spans_per_trace",
            "value": 3,
            "unit": "spans"
          },
          {
            "name": "recommendation/baseline/trace_bytes",
            "value": 1171,
            "unit": "bytes"
          },
          {
            "name": "recommendation/baseline/otlp_bytes_per_request",
            "value": 1174.9,
            "unit": "bytes"
          },
          {
            "name": "email/muzzle-0/spans_per_trace",
            "value": 1,
            "unit": "spans"
          },
          {
            "name": "email/muzzle-0/trace_bytes",
            "value": 359,
            "unit": "bytes"
          },
          {
            "name": "email/muzzle-0/otlp_bytes_per_request",
            "value": 363.4,
            "unit": "bytes"
          },
          {
            "name": "payment/muzzle-0/spans_per_trace",
            "value": 1,
            "unit": "spans"
          },
          {
            "name": "payment/muzzle-0/trace_bytes",
            "value": 244,
            "unit": "bytes"
          },
          {
            "name": "payment/muzzle-0/otlp_bytes_per_request",
            "value": 253,
            "unit": "bytes"
          },
          {
            "name": "recommendation/muzzle-0/spans_per_trace",
            "value": 2,
            "unit": "spans"
          },
          {
            "name": "recommendation/muzzle-0/trace_bytes",
            "value": 623,
            "unit": "bytes"
          },
          {
            "name": "recommendation/muzzle-0/otlp_bytes_per_request",
            "value": 626.7,
            "unit": "bytes"
          },
          {
            "name": "email/muzzle-10/spans_per_trace",
            "value": 1.315,
            "unit": "spans"
          },
          {
            "name": "email/muzzle-10/trace_bytes",
            "value": 396.1,
            "unit": "bytes"
          },
          {
            "name": "email/muzzle-10/otlp_bytes_per_request",
            "value": 401.1,
            "unit": "bytes"
          },
          {
            "name": "payment/muzzle-10/spans_per_trace",
            "value": 1.096,
            "unit": "spans"
          },
          {
            "name": "payment/muzzle-10/trace_bytes",
            "value": 264.7,
            "unit": "bytes"
          },
          {
            "name": "payment/muzzle-10/otlp_bytes_per_request",
            "value": 273.8,
            "unit": "bytes"
          },
          {
            "name": "recommendation/muzzle-10/spans_per_trace",
            "value": 2.113,
            "unit": "spans"
          },
          {
            "name": "recommendation/muzzle-10/trace_bytes",
            "value": 684.9,
            "unit": "bytes"
          },
          {
            "name": "recommendation/muzzle-10/otlp_bytes_per_request",
            "value": 688.8,
            "unit": "bytes"
          },
          {
            "name": "email/muzzle-100/spans_per_trace",
            "value": 4,
            "unit": "spans"
          },
          {
            "name": "email/muzzle-100/trace_bytes",
            "value": 712,
            "unit": "bytes"
          },
          {
            "name": "email/muzzle-100/otlp_bytes_per_request",
            "value": 717,
            "unit": "bytes"
          },
          {
            "name": "payment/muzzle-100/spans_per_trace",
            "value": 2,
            "unit": "spans"
          },
          {
            "name": "payment/muzzle-100/trace_bytes",
            "value": 459,
            "unit": "bytes"
          },
          {
            "name": "payment/muzzle-100/otlp_bytes_per_request",
            "value": 468.1,
            "unit": "bytes"
          },
          {
            "name": "recommendation/muzzle-100/spans_per_trace",
            "value": 3,
            "unit": "spans"
          },
          {
            "name": "recommendation/muzzle-100/trace_bytes",
            "value": 1171,
            "unit": "bytes"
          },
          {
            "name": "recommendation/muzzle-100/otlp_bytes_per_request",
            "value": 1174.5,
            "unit": "bytes"
          },
          {
            "name": "email/header-10/spans_per_trace",
            "value": 1.282,
            "unit": "spans"
          },
          {
            "name": "email/header-10/trace_bytes",
            "value": 399.1,
            "unit": "bytes"
          },
          {
            "name": "email/header-10/otlp_bytes_per_request",
            "value": 404.1,
            "unit": "bytes"
          },
          {
            "name": "payment/header-10/spans_per_trace",
            "value": 1.096,
            "unit": "spans"
          },
          {
            "name": "payment/header-10/trace_bytes",
            "value": 268.4,
            "unit": "bytes"
          },
          {
            "name": "payment/header-10/otlp_bytes_per_request",
            "value": 277.6,
            "unit": "bytes"
          },
          {
            "name": "recommendation/header-10/spans_per_trace",
            "value": 2.114,
            "unit": "spans"
          },
          {
            "name": "recommendation/header-10/trace_bytes",
            "value": 691.9,
            "unit": "bytes"
          },
          {
            "name": "recommendation/header-10/otlp_bytes_per_request",
            "value": 695.7,
            "unit": "bytes"
          }
        ]
      }
    ]
  }
}