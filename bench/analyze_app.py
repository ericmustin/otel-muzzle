#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Summarise an otel-muzzle benchmark run on a real application (bench/gitlab/run.sh).

Input: a run directory:

    <run>/run.json                     run parameters and the ordered list of variants
    <run>/<variant>/measure.pb         collector file exporter output for the measured phase
    <run>/<variant>/k6-measure.json    k6 summary of the measured phase

Every trace in measure.pb is one of:
- request: a root SERVER span answered a k6 request (User-Agent muzzle-bench-k6);
- background: any other root, such as a Sidekiq job, a cron job, or the application calling itself;
- cut: no root in the file, because the measured window opened or closed while the trace was running.

The headline counts everything the application exported in the window, per k6 request: that is what an APM
bill sees. Checks: no span in a request or background trace points at a missing parent; no non-verbose trace
kept an INTERNAL span under a local parent; spans of every other kind, by request type, and the INTERNAL spans of
verbose traces, relative to their request type in stock, stay within the spread of two stock runs; the two stock
runs agree on the headline; and no HTTP call left the Compose network, in the warmup or the measured window.

Output: results.md and results.json in the run directory. Standard library only.

Usage: analyze_app.py <run-dir>
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from collections import Counter, defaultdict
from math import sqrt
from statistics import mean, pvariance
from urllib.parse import urlsplit

from analyze import KIND, Span, fmt_bytes, fmt_pct, forced_verbose, parse, pct_change, ratio_selects

K6_AGENT = "muzzle-bench-k6"
INTERNAL, SERVER, CLIENT = 1, 2, 3
KEPT_KINDS = (2, 3, 4, 5)  # SERVER, CLIENT, PRODUCER, CONSUMER: otel-muzzle never drops these
TOP_TYPES = 12
VERBOSE_INTERNAL = "verbose request traces|internal, relative to stock"
STOCK_BOUND = 0.03  # how far apart the two stock runs' headline numbers may be


PROCESSES = ("web", "sidekiq", "other")


def process_of(span: Span) -> str:
    """web (Puma), sidekiq, or other (rake tasks and `rails runner`, which GitLab runs at boot and on demand)."""
    command = span.resource.get("process.command", "")
    if "puma" in command:
        return "web"
    return "sidekiq" if "sidekiq" in command else "other"


def roots(trace: list[Span]) -> list[Span]:
    """Spans with no parent, and SERVER spans whose parent came over the wire."""
    ids = {s.span_id for s in trace}
    return [s for s in trace if not s.parent_id or (s.kind == SERVER and s.parent_id not in ids)]


def _user_agent(span: Span) -> str:
    return span.attributes.get("user_agent.original") or span.attributes.get("http.user_agent", "")


def classify(trace: list[Span]) -> str:
    found = roots(trace)
    if not found:
        return "cut"
    if any(s.kind == SERVER and _user_agent(s).startswith(K6_AGENT) for s in found):
        return "request"
    return "background"


def requests_in(trace: list[Span]) -> list[list[Span]]:
    """Each k6 request in a trace: its root SERVER span and every span under it. Workhorse passes a forced
    traceparent to a request's /authorize preflight too, so one trace can hold two."""
    children: dict[bytes, list[Span]] = defaultdict(list)
    for s in trace:
        children[s.parent_id].append(s)
    found = []
    for root in roots(trace):
        if root.kind == SERVER and _user_agent(root).startswith(K6_AGENT):
            request, todo = [], [root]
            while todo:
                request.append(todo.pop())
                todo.extend(children[request[-1].span_id])
            found.append(request)
    return found


def request_type(trace: list[Span]) -> str:
    """Controller#action for a Rails request, whose route can be as vague as /*namespace_id/:id; otherwise the
    root span's name, which for an API request carries the method and route."""
    root = next(s for s in roots(trace) if s.kind == SERVER)
    controller, action = root.attributes.get("code.namespace"), root.attributes.get("code.function")
    return f"{controller}#{action}" if controller and action else root.name


def is_verbose(trace_id: bytes, trace: list[Span], ratio: float) -> bool:
    return ratio_selects(trace_id, ratio) or any(forced_verbose(s.trace_state) for s in trace)


def violations(trace: list[Span]) -> int:
    """INTERNAL spans whose parent is a span of the same process: otel-muzzle drops these outside verbose traces."""
    by_id = {s.span_id: s for s in trace}
    return sum(
        1
        for s in trace
        if s.kind == INTERNAL and s.parent_id in by_id and by_id[s.parent_id].resource == s.resource
    )


def dangling(trace: list[Span]) -> int:
    """Spans other than roots whose parent was never exported."""
    ids = {s.span_id for s in trace}
    return sum(1 for s in trace if s.parent_id and s.parent_id not in ids and s.kind != SERVER)


def _local(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # a Compose service name, or localhost
    return ip.is_private or ip.is_loopback


def outbound(spans: list[Span]) -> Counter:
    """HTTP calls to anything outside the Compose network, by peer. The benchmark must not send data out."""
    peers: Counter = Counter()
    for s in spans:
        a = s.attributes
        if s.kind != CLIENT or not (a.get("http.method") or a.get("http.request.method")):
            continue
        peer = (a.get("server.address") or a.get("net.peer.name")
                or urlsplit(a.get("url.full") or a.get("http.url", "")).hostname or "")
        if not _local(peer):
            peers[peer] += 1
    return peers


def stock_disagreement(ref: dict, other: dict, bound: float = STOCK_BOUND) -> list[str]:
    """Headline numbers on which a second stock run differs from the first by more than bound: something other
    than otel-muzzle moved them, such as a burst of background work in one window."""
    return [f"{key.replace('_', ' ')}: {other[key]:,.2f} against {ref[key]:,.2f} in the first stock run"
            for key in ("spans_per_request", "bytes_per_request")
            if abs(other[key] - ref[key]) > bound * ref[key]]


def spread_failures(base1: dict, base2: dict, variant: dict, min_traces: int = 20) -> list[str]:
    """Keys whose mean count per trace moved further from the stock runs than the stock runs moved from each
    other, or than three standard errors of sampling noise, whichever is larger."""
    failures = []
    for key in sorted(variant):
        b1, b2, v = base1.get(key, []), base2.get(key, []), variant[key]
        if min(len(b1), len(b2), len(v)) < min_traces:
            continue
        b = b1 + b2
        tolerance = max(abs(mean(b1) - mean(b2)), 3 * sqrt(pvariance(b) / len(b) + pvariance(v) / len(v)))
        if abs(mean(v) - mean(b)) > tolerance + 1e-9:
            failures.append(f"{key}: {mean(v):.2f} vs baseline {mean(b):.2f} (tolerance {tolerance:.2f})")
    return failures


def spread_samples(records: list, stock_internal: dict) -> dict[str, list[float]]:
    """The values spread_failures compares, from (request type, Counter of span kinds, verbose) per request trace.

    Returns lists keyed by "<request type>|<kind>": one count per request trace, for each of the kept kinds
    (SERVER, CLIENT, PRODUCER, CONSUMER), type by type, because a run's random mix of request types moves any
    pooled mean. The key VERBOSE_INTERNAL holds the INTERNAL span count of each verbose trace, divided by its
    request type's stock mean (stock_internal): too few verbose traces fall in any one type to compare them by
    type, and the ratio keeps the mix of types from passing for a change. Types with no stock INTERNAL spans
    add nothing to it."""
    samples: dict[str, list[float]] = defaultdict(list)
    for rtype, kinds, verbose in records:
        for kind in KEPT_KINDS:
            samples[f"{rtype}|{KIND[kind]}"].append(kinds[kind])
        if verbose and stock_internal.get(rtype):
            samples[VERBOSE_INTERNAL].append(kinds[INTERNAL] / stock_internal[rtype])
    return dict(samples)


# --------------------------------------------------------------------------------------------
# One variant
# --------------------------------------------------------------------------------------------


def variant_stats(variant_dir: str, ratio: float | None) -> tuple[dict, list]:
    """Return the variant's numbers, and (request type, span kinds, verbose) per request trace for the spread check.

    ratio is the variant's OTEL_MUZZLE_VERBOSE_RATIO, or None for the stock image (no rule to check)."""
    spans, bytes_by_service = parse(os.path.join(variant_dir, "measure.pb"))
    with open(os.path.join(variant_dir, "k6-measure.json")) as fh:
        k6 = json.load(fh)

    calls = outbound(spans)
    warmup = os.path.join(variant_dir, "warmup.pb")
    if os.path.exists(warmup):
        calls += outbound(parse(warmup)[0])

    traces: dict[bytes, list[Span]] = defaultdict(list)
    for s in spans:
        traces[s.trace_id].append(s)

    classes: Counter = Counter()
    by_type: dict[str, dict] = defaultdict(lambda: {"spans": [], "bytes": []})
    background: list[int] = []
    records: list = []
    bad_parents = rule_violations = 0
    for trace_id, trace in traces.items():
        cls = classify(trace)
        classes[cls] += 1
        if cls == "cut":
            continue
        bad_parents += dangling(trace)
        verbose = ratio is None or is_verbose(trace_id, trace, ratio)
        if not verbose:
            rule_violations += violations(trace)
        if cls == "background":
            background.append(len(trace))
            continue
        for request in requests_in(trace):
            rtype = request_type(request)
            by_type[rtype]["spans"].append(len(request))
            by_type[rtype]["bytes"].append(sum(s.size for s in request))
            records.append((rtype, Counter(s.kind for s in request), verbose))

    requests = int(k6["http_reqs"])
    otlp_bytes = sum(bytes_by_service.values())
    stats = {
        "k6": k6,
        "spans": len(spans),
        "otlp_bytes": otlp_bytes,
        "spans_per_request": len(spans) / requests if requests else 0.0,
        "bytes_per_request": otlp_bytes / requests if requests else 0.0,
        "spans_per_request_by_process": {
            p: n / requests if requests else 0.0 for p, n in sorted(Counter(process_of(s) for s in spans).items())
        },
        "spans_per_request_by_kind": {
            KIND[k]: n / requests if requests else 0.0 for k, n in sorted(Counter(s.kind for s in spans).items())
        },
        "traces": {c: classes[c] for c in ("request", "background", "cut")},
        "request_types": {
            t: {"traces": len(v["spans"]), "spans_per_trace": mean(v["spans"]), "bytes_per_trace": mean(v["bytes"])}
            for t, v in by_type.items()
        },
        "background": {
            "traces": len(background),
            "spans_per_trace": mean(background) if background else 0.0,
        },
        "checks": {"dangling": bad_parents, "violations": rule_violations if ratio is not None else None,
                   "outbound": dict(calls)},
    }
    return stats, records


def build(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "run.json")) as fh:
        run = json.load(fh)
    variants, records = [], []
    for v in run["variants"]:
        ratio = None if v["images"] == "stock" else float(v["ratio"])
        stats, trace_records = variant_stats(os.path.join(run_dir, v["name"]), ratio)
        variants.append({**v, **stats})
        records.append(trace_records)

    stock = [i for i, v in enumerate(variants) if v["images"] == "stock"]
    if len(stock) < 2:
        raise SystemExit("analyze_app.py needs two stock variants: the second measures the run-to-run spread")
    internal_by_type: dict[str, list[int]] = defaultdict(list)
    for rtype, kinds, _ in records[stock[0]] + records[stock[1]]:
        internal_by_type[rtype].append(kinds[INTERNAL])
    stock_internal = {t: mean(counts) for t, counts in internal_by_type.items()}
    samples = [spread_samples(r, stock_internal) for r in records]
    for i, (v, variant_samples) in enumerate(zip(variants, samples)):
        checks = v["checks"]
        if v["images"] == "stock":
            checks["spread_failures"] = stock_disagreement(variants[stock[0]], v) if i != stock[0] else []
        else:
            checks["spread_failures"] = spread_failures(samples[stock[0]], samples[stock[1]], variant_samples)
        checks["k6_checks_rate"] = v["k6"]["checks_rate"]
        checks["passed"] = (
            checks["dangling"] == 0
            and not checks["violations"]
            and not checks["spread_failures"]
            and not checks["outbound"]
            and checks["k6_checks_rate"] == 1
        )
    return {"run": run, "reference": variants[stock[0]]["name"], "variants": variants,
            "passed": all(v["checks"]["passed"] for v in variants)}


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------


def _vs(value: float, base: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f} ({fmt_pct(pct_change(value, base))})"


def _bytes_vs(value: float, base: float) -> str:
    return f"{fmt_bytes(value)} ({fmt_pct(pct_change(value, base))})"


def markdown(results: dict) -> str:
    run, variants = results["run"], results["variants"]
    ref = next(v for v in variants if v["name"] == results["reference"])
    muzzled = [v for v in variants if v["images"] != "stock"]
    compare = next((v for v in muzzled if v["name"] == "muzzle-10"), muzzled[0] if muzzled else None)
    out: list[str] = []
    w = out.append

    w("# otel-muzzle on GitLab\n")
    requests = ", ".join(f"{v['name']} {v['k6']['http_reqs']:,}" for v in variants)
    w(f"Run `{run['run_id']}`: GitLab CE {run['gitlab_version']}, k6 at {run['rate']} iterations/s for "
      f"{run['duration']} per variant, after a warmup that is not counted. k6 requests: {requests}.\n")

    w("## Spans and bytes per request\n")
    w("Everything GitLab exported during the measured window (web requests, Sidekiq jobs, cron and polling), "
      "divided by the k6 requests in it, and the change against GitLab's own tracing. OTLP bytes are the export "
      "requests as the collector received them, uncompressed.\n")
    w("| Variant | Spans per request | OTLP bytes per request |")
    w("|---|---:|---:|")
    for v in variants:
        w(f"| {v['label']} | {_vs(v['spans_per_request'], ref['spans_per_request'])} "
          f"| {_bytes_vs(v['bytes_per_request'], ref['bytes_per_request'])} |")
    w("")

    w("## By process\n")
    has_other = any(v["spans_per_request_by_process"].get("other") for v in variants)
    processes = PROCESSES if has_other else PROCESSES[:-1]
    w("Spans per k6 request from the web server (Puma)"
      + (", the background workers (Sidekiq), and anything else (rake tasks and `rails runner`).\n" if has_other
         else " and the background workers (Sidekiq).\n"))
    w("| Variant | " + " | ".join(p.capitalize() for p in processes) + " |")
    w("|---|" + "---:|" * len(processes))
    for v in variants:
        by, base = v["spans_per_request_by_process"], ref["spans_per_request_by_process"]
        w("| " + v["label"] + " | "
          + " | ".join(_vs(by.get(p, 0.0), base.get(p, 0.0)) for p in processes) + " |")
    w("")

    w("## By kind\n")
    w("Spans per k6 request by span kind. otel-muzzle drops only INTERNAL spans.\n")
    kinds = [KIND[k] for k in (INTERNAL, *KEPT_KINDS)]
    w("| Variant | " + " | ".join(k.capitalize() for k in kinds) + " |")
    w("|---|" + "---:|" * len(kinds))
    for v in variants:
        by, base = v["spans_per_request_by_kind"], ref["spans_per_request_by_kind"]
        w("| " + v["label"] + " | " + " | ".join(_vs(by.get(k, 0.0), base.get(k, 0.0)) for k in kinds) + " |")
    w("")

    if compare:  # a stock-only run has nothing to compare
        w("## Request traces by type\n")
        w(f"Traces started by a k6 request, by their root span, with {compare['label']} against "
          f"{ref['label']}. Trace bytes are the encoded spans of the trace, without the resource and scope each "
          "export repeats.\n")
        w("| Request | Traces | Spans per trace, stock | with otel-muzzle | Trace bytes, stock | with otel-muzzle |")
        w("|---|---:|---:|---:|---:|---:|")
        ref_types, cmp_types = ref["request_types"], compare["request_types"]
        top = sorted(ref_types, key=lambda t: -ref_types[t]["traces"])[:TOP_TYPES]

        def pooled(types: dict, names) -> dict:
            rows = [types[t] for t in names if t in types]
            n = sum(r["traces"] for r in rows)
            return {
                "traces": n,
                "spans_per_trace": sum(r["spans_per_trace"] * r["traces"] for r in rows) / n if n else 0.0,
                "bytes_per_trace": sum(r["bytes_per_trace"] * r["traces"] for r in rows) / n if n else 0.0,
            }

        rest = [t for t in set(ref_types) | set(cmp_types) if t not in top]
        rows = [(t, ref_types.get(t), cmp_types.get(t)) for t in top]
        rows.append(("other", pooled(ref_types, rest), pooled(cmp_types, rest)))
        rows.append(("**all requests**", pooled(ref_types, ref_types), pooled(cmp_types, cmp_types)))
        for name, r, c in rows:
            if not r or not c or not r["traces"] or not c["traces"]:
                continue
            label = name if name.startswith("**") or name == "other" else f"`{name}`"
            w(f"| {label} | {r['traces']:,} | {r['spans_per_trace']:.2f} | "
              f"{_vs(c['spans_per_trace'], r['spans_per_trace'])} | {fmt_bytes(r['bytes_per_trace'])} | "
              f"{_bytes_vs(c['bytes_per_trace'], r['bytes_per_trace'])} |")
        w("")

    w("## Background traces\n")
    w("Traces no k6 request started directly: Sidekiq jobs (each its own trace, linked to the request that queued "
      "it), cron jobs, polling, and GitLab's internal API calls from Gitaly.\n")
    w("| Variant | Traces | Spans per trace |")
    w("|---|---:|---:|")
    for v in variants:
        w(f"| {v['label']} | {v['background']['traces']:,} "
          f"| {_vs(v['background']['spans_per_trace'], ref['background']['spans_per_trace'])} |")
    w("")

    w("## Checks\n")
    w("- *Cut* traces have no root in the window, because it opened or closed while they ran; they are left out of "
      "the other checks.\n"
      "- *Dangling* spans point at a parent that was never exported. Must be 0.\n"
      "- *Rule violations* are INTERNAL spans under a local parent in a trace that is not verbose. Must be 0.\n"
      "- *Spread* compares SERVER, CLIENT, PRODUCER and CONSUMER spans per request trace, by request type, and the "
      "INTERNAL spans of verbose traces, relative to their request type's stock mean, with the two stock runs. "
      "A failure is a mean further from stock than the "
      "stock runs are from each other, or than three standard errors, whichever is larger. The second stock run "
      f"must also be within {STOCK_BOUND:.0%} of the first on spans and on OTLP bytes per request.\n"
      "- *Outbound calls* are HTTP calls to anything outside the Compose network, in the warmup or the measured "
      "window. Must be 0.\n"
      "- *k6 checks* must all pass.\n")
    w("| Variant | Request traces | Background traces | Cut | Dangling | Rule violations | Spread failures "
      "| Outbound calls | k6 checks |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in variants:
        c, t = v["checks"], v["traces"]
        rule = "n/a" if c["violations"] is None else f"{c['violations']:,}"
        spread = "n/a" if v["name"] == ref["name"] else str(len(c["spread_failures"]))
        w(f"| {v['label']} | {t['request']:,} | {t['background']:,} | {t['cut']:,} | {c['dangling']:,} | {rule} "
          f"| {spread} | {sum(c['outbound'].values()):,} | {c['k6_checks_rate'] * 100:.1f}% |")
    w("")
    failures = [f"- {v['label']}: {line}" for v in variants for line in v["checks"]["spread_failures"]]
    failures += [f"- {v['label']}: {n:,} HTTP calls to {peer}" for v in variants
                 for peer, n in v["checks"]["outbound"].items()]
    if failures:
        w("\n".join(failures) + "\n")
    w("Every check passed." if results["passed"] else "**Some checks failed; these numbers are not a result.**")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    run_dir = argv[0]
    results = build(run_dir)
    with open(os.path.join(run_dir, "results.json"), "w") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")
    report = markdown(results)
    with open(os.path.join(run_dir, "results.md"), "w") as fh:
        fh.write(report)
    print(report, end="")
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
