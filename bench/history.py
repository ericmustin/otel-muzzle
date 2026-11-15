#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a benchmark run's results.json into input for github-action-benchmark's customSmallerIsBetter tool.

Each entry is one measure of one variant, named `<benchmark>/<variant>/<measure>`: spans and bytes per request or
per trace, so smaller is better. The benchmark is the OpenTelemetry Demo service, or `gitlab`. The dashboard in
site/ reads the names back.

Usage:
    history.py <run-dir>/results.json > benchmark.json

A run whose checks failed is refused, so it never enters the history. Standard library only.
"""

from __future__ import annotations

import json
import sys

from analyze import SERVICES

UNITS = {"spans_per_request": "spans", "spans_per_trace": "spans", "trace_bytes": "bytes",
         "otlp_bytes_per_request": "bytes"}


def entry(benchmark: str, variant: str, measure: str, value: float) -> dict:
    return {"name": f"{benchmark}/{variant}/{measure}", "unit": UNITS[measure], "value": round(value, 3)}


def demo(rows: list[dict]):
    """analyze.py rows: one per variant and service. The Go control service never gets otel-muzzle."""
    for r in rows:
        if r["service"] in SERVICES:
            yield entry(r["service"], r["variant"], "spans_per_trace", r["spans_per_trace"])
            yield entry(r["service"], r["variant"], "trace_bytes", r["trace_bytes_mean"])
            yield entry(r["service"], r["variant"], "otlp_bytes_per_request", r["otlp_bytes_per_request"])


def gitlab(variants: list[dict]):
    """analyze_app.py variants: everything GitLab exported per k6 request, then the request traces alone."""
    for v in variants:
        types = v["request_types"].values()
        traces = sum(t["traces"] for t in types)
        spans = sum(t["spans_per_trace"] * t["traces"] for t in types)
        span_bytes = sum(t["bytes_per_trace"] * t["traces"] for t in types)
        yield entry("gitlab", v["name"], "spans_per_request", v["spans_per_request"])
        yield entry("gitlab", v["name"], "otlp_bytes_per_request", v["bytes_per_request"])
        yield entry("gitlab", v["name"], "spans_per_trace", spans / traces)
        yield entry("gitlab", v["name"], "trace_bytes", span_bytes / traces)


def entries(results: dict) -> list[dict]:
    if not results.get("passed"):
        raise SystemExit("the run's checks failed (or it predates them), so it is not a result")
    return list(demo(results["rows"]) if "rows" in results else gitlab(results["variants"]))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[0]) as fh:
        json.dump(entries(json.load(fh)), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
