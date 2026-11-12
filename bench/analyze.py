#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Summarise an otel-muzzle benchmark run: spans, trace size and OTLP bytes per service and variant.

Input: a run directory written by run.sh:

    <run>/run.json                     run parameters and the ordered list of variants
    <run>/<variant>/measure.pb         collector file exporter output (format: proto, no batching)
    <run>/<variant>/k6-measure.json    k6 per-service request counts and latency

Each record in measure.pb is one SDK export request, re-encoded by the collector as OTLP
protobuf and prefixed with its 4-byte big-endian length. Its size is the "bytes ingested".

For every service and trace it also checks otel-muzzle's rule: a trace keeps its INTERNAL spans exactly when
the variant's ratio selects its trace id or its tracestate carries muzzle=verbose.

Output: results.md, results.csv, and results.json in the run directory. Standard library only.

Usage:
    analyze.py <run-dir>                  write the reports
    analyze.py --dump <variant-dir>       print span name/kind counts per service
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

SERVICES = ("email", "payment", "recommendation")
LANGUAGE = {"email": "Ruby", "payment": "Node.js", "recommendation": "Python", "product-catalog": "Go"}
CONTROL = "product-catalog"  # called by recommendation; never gets otel-muzzle
KIND = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}

# --------------------------------------------------------------------------------------------
# Protobuf wire format: just enough of opentelemetry/proto/trace/v1/trace.proto
# --------------------------------------------------------------------------------------------


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf: bytes):
    """Yield (field_number, wire_type, value) for one message; LEN values are bytes."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i : i + 8], i + 8
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i : i + length], i + length
        elif wire == 5:
            value, i = buf[i : i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield number, wire, value


def _varint_size(value: int) -> int:
    size = 1
    while value >= 0x80:
        value >>= 7
        size += 1
    return size


def _string_attributes(kv_messages) -> dict[str, str]:
    """KeyValue{1: key, 2: AnyValue{1: string_value}} -> {key: value} for string values."""
    out = {}
    for kv in kv_messages:
        key = value = None
        for number, _, v in _fields(kv):
            if number == 1:
                key = v.decode()
            elif number == 2:
                for n2, _, v2 in _fields(v):
                    if n2 == 1:
                        value = v2.decode()
        if key is not None and value is not None:
            out[key] = value
    return out


@dataclass
class Span:
    service: str
    scope: str
    trace_id: bytes
    span_id: bytes
    parent_id: bytes
    name: str
    kind: int
    trace_state: str
    size: int  # encoded bytes of this span inside its ScopeSpans: tag + length + body
    attributes: dict = field(default_factory=dict)  # the span's string-valued attributes
    resource: dict = field(default_factory=dict)  # the resource's string-valued attributes, shared per export


def ratio_selects(trace_id: bytes, ratio: float) -> bool:
    """otel-muzzle's rule: trace-id bytes 12-15, as a big-endian uint32, below floor(ratio * 2**32)."""
    return int.from_bytes(trace_id[12:16], "big") < int(ratio * 2**32)


def forced_verbose(trace_state: str) -> bool:
    return any(member.strip() == "muzzle=verbose" for member in trace_state.split(","))


def read_records(path: str):
    """Yield the protobuf payload of each length-prefixed record in a file exporter output."""
    with open(path, "rb") as fh:
        data = fh.read()
    i = 0
    while i + 4 <= len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        if i + 4 + length > len(data):
            break  # partial trailing record: collector stopped mid-write
        yield data[i + 4 : i + 4 + length]
        i += 4 + length


def parse(path: str) -> tuple[list[Span], dict[str, int]]:
    """Return every span in the file and the OTLP bytes per service."""
    spans: list[Span] = []
    bytes_by_service: dict[str, int] = defaultdict(int)
    for record in read_records(path):
        for number, _, resource_spans in _fields(record):
            if number != 1:
                continue
            resource: dict[str, str] = {}
            scope_spans = []
            for n, _, v in _fields(resource_spans):
                if n == 1:  # Resource
                    resource = _string_attributes(v2 for n2, _, v2 in _fields(v) if n2 == 1)
                elif n == 2:
                    scope_spans.append(v)
            service = resource.get("service.name", "unknown")
            # Size of this ResourceSpans inside the export request: tag + length + body.
            bytes_by_service[service] += 1 + _varint_size(len(resource_spans)) + len(resource_spans)
            for ss in scope_spans:
                # Protobuf fields may come in any order (the collector writes spans before the
                # scope), so read the scope first and the spans second.
                scope, raw_spans = "", []
                for n, _, v in _fields(ss):
                    if n == 1:
                        scope = next((v2.decode() for n2, _, v2 in _fields(v) if n2 == 1), "")
                    elif n == 2:
                        raw_spans.append(v)
                spans.extend(_span(service, scope, raw, resource) for raw in raw_spans)
    return spans, dict(bytes_by_service)


def _span(service: str, scope: str, buf: bytes, resource: dict[str, str]) -> Span:
    trace_id = span_id = parent_id = b""
    name, kind, trace_state = "", 0, ""
    attribute_kvs = []
    for n, _, v in _fields(buf):
        if n == 1:
            trace_id = bytes(v)
        elif n == 2:
            span_id = bytes(v)
        elif n == 3:
            trace_state = v.decode()
        elif n == 4:
            parent_id = bytes(v)
        elif n == 5:
            name = v.decode()
        elif n == 6:
            kind = v
        elif n == 9:
            attribute_kvs.append(v)
    size = 1 + _varint_size(len(buf)) + len(buf)
    return Span(service, scope, trace_id, span_id, parent_id, name, kind, trace_state, size,
                _string_attributes(attribute_kvs), resource)


# --------------------------------------------------------------------------------------------
# Per-variant statistics
# --------------------------------------------------------------------------------------------


@dataclass
class ServiceStats:
    requests: int = 0
    spans: int = 0
    bytes: int = 0
    span_bytes: int = 0
    kinds: Counter = field(default_factory=Counter)
    server_spans: int = 0
    traces: int = 0
    verbose_traces: int = 0  # traces that kept their INTERNAL spans in this service
    trace_sizes: list = field(default_factory=list)  # encoded span bytes per trace, in this service
    mismatches: int = 0  # traces whose INTERNAL spans disagree with otel-muzzle's rule
    dangling: int = 0  # non-server spans whose parent was never exported
    ok_rate: float | None = None
    forced_requests: int = 0
    latency_ms: dict = field(default_factory=dict)

    @property
    def spans_per_request(self) -> float:
        return self.spans / self.requests if self.requests else 0.0

    @property
    def bytes_per_request(self) -> float:
        return self.bytes / self.requests if self.requests else 0.0

    @property
    def spans_per_trace(self) -> float:
        return self.spans / self.traces if self.traces else 0.0

    @property
    def trace_bytes(self) -> float:
        """Mean encoded span bytes per trace: the trace's size without resource and scope overhead."""
        return self.span_bytes / self.traces if self.traces else 0.0

    @property
    def trace_bytes_p95(self) -> float:
        sizes = sorted(self.trace_sizes)
        return float(sizes[min(len(sizes) - 1, int(0.95 * len(sizes)))]) if sizes else 0.0

    @property
    def verbose_share(self) -> float:
        return self.verbose_traces / self.traces if self.traces else 0.0


def variant_stats(variant_dir: str, ratio: float | None) -> dict[str, ServiceStats]:
    """ratio is the variant's OTEL_MUZZLE_VERBOSE_RATIO, or None for stock images (no rule to check)."""
    spans, bytes_by_service = parse(os.path.join(variant_dir, "measure.pb"))
    with open(os.path.join(variant_dir, "k6-measure.json")) as fh:
        k6 = json.load(fh)

    exported_ids = {(s.trace_id, s.span_id) for s in spans}
    stats: dict[str, ServiceStats] = defaultdict(ServiceStats)
    by_trace: dict[tuple[str, bytes], list[Span]] = defaultdict(list)
    for s in spans:
        st = stats[s.service]
        st.spans += 1
        st.span_bytes += s.size
        st.kinds[KIND.get(s.kind, str(s.kind))] += 1
        by_trace[(s.service, s.trace_id)].append(s)
        if s.kind == 2:
            st.server_spans += 1
        elif s.parent_id and (s.trace_id, s.parent_id) not in exported_ids:
            st.dangling += 1
    for (service, trace_id), trace_spans in by_trace.items():
        st = stats[service]
        st.traces += 1
        st.trace_sizes.append(sum(s.size for s in trace_spans))
        kept_internal = any(s.kind == 1 for s in trace_spans)
        st.verbose_traces += kept_internal
        if ratio is not None and service != CONTROL:
            expected = ratio_selects(trace_id, ratio) or any(forced_verbose(s.trace_state) for s in trace_spans)
            st.mismatches += kept_internal != expected
    for service, size in bytes_by_service.items():
        stats[service].bytes = size

    for service, row in k6["services"].items():
        st = stats[service]
        st.requests = int(row["requests"])
        st.ok_rate = row["ok_rate"]
        st.forced_requests = int(row["forced_verbose_requests"])
        st.latency_ms = row["latency_ms"]
    # The control service answers exactly one call per recommendation request.
    stats[CONTROL].requests = stats["recommendation"].requests
    return dict(stats)


def dump(variant_dir: str) -> None:
    spans, bytes_by_service = parse(os.path.join(variant_dir, "measure.pb"))
    shapes = Counter((s.service, KIND.get(s.kind), s.scope, s.name) for s in spans)
    for (service, kind, scope, name), count in sorted(shapes.items()):
        print(f"{service:16} {kind:9} {count:6}  {name}  [{scope}]")
    for service, size in sorted(bytes_by_service.items()):
        print(f"{service:16} bytes {size}")


# --------------------------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------------------------


def pct_change(new: float, base: float) -> float | None:
    return (new - base) / base * 100.0 if base else None


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}%"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1000.0
    return f"{n:.2f} TB"


def build(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "run.json")) as fh:
        run = json.load(fh)
    results = {"run": run, "variants": []}
    for variant in run["variants"]:
        ratio = None if variant["images"] == "stock" else float(variant["ratio"])
        stats = variant_stats(os.path.join(run_dir, variant["name"]), ratio)
        results["variants"].append({"variant": variant, "stats": stats})
    return results


def baseline_of(results: dict) -> dict[str, ServiceStats]:
    for entry in results["variants"]:
        if entry["variant"]["images"] == "stock":
            return entry["stats"]
    raise SystemExit("run has no baseline (stock images) variant")


def rows(results: dict):
    """One flat row per (variant, service): the CSV/JSON shape."""
    base = baseline_of(results)
    for entry in results["variants"]:
        v = entry["variant"]
        for service in (*SERVICES, CONTROL):
            st = entry["stats"].get(service, ServiceStats())
            b = base.get(service, ServiceStats())
            yield {
                "variant": v["name"],
                "label": v["label"],
                "images": v["images"],
                "verbose_ratio": v["ratio"] if v["images"] != "stock" else "",
                "header_ratio": v["header_ratio"],
                "service": service,
                "language": LANGUAGE[service],
                "requests": st.requests,
                "ok_rate": round(st.ok_rate, 4) if st.ok_rate is not None else "",
                "spans": st.spans,
                "traces": st.traces,
                "spans_per_request": round(st.spans_per_request, 3),
                "spans_per_trace": round(st.spans_per_trace, 3),
                "trace_bytes_mean": round(st.trace_bytes, 1),
                "trace_bytes_p95": round(st.trace_bytes_p95, 1),
                "trace_bytes_change_pct": _round(pct_change(st.trace_bytes, b.trace_bytes)),
                "otlp_bytes": st.bytes,
                "otlp_bytes_per_request": round(st.bytes_per_request, 1),
                "span_change_pct": _round(pct_change(st.spans_per_request, b.spans_per_request)),
                "byte_change_pct": _round(pct_change(st.bytes_per_request, b.bytes_per_request)),
                "server_spans": st.server_spans,
                "internal_spans": st.kinds.get("internal", 0),
                "client_spans": st.kinds.get("client", 0),
                "verbose_trace_share": round(st.verbose_share, 4),
                "rule_mismatches": st.mismatches,
                "dangling_parent_refs": st.dangling,
                "latency_p50_ms": _round(st.latency_ms.get("p50")),
                "latency_p95_ms": _round(st.latency_ms.get("p95")),
            }


def _round(x, digits=2):
    return round(x, digits) if isinstance(x, (int, float)) else ""


def _matrix(w, variants, base, metric, fmt) -> None:
    """One row per variant, one column per service plus all three: value and change versus stock."""
    w("| Variant | " + " | ".join(f"{s} ({LANGUAGE[s]})" for s in SERVICES) + " | All three |")
    w("|---|" + "---:|" * (len(SERVICES) + 1))
    for entry in variants:
        st = entry["stats"]
        cells = [f"{fmt(metric(st[s]))} ({fmt_pct(pct_change(metric(st[s]), metric(base[s])))})" for s in SERVICES]
        total = sum(metric(st[s]) for s in SERVICES)
        base_total = sum(metric(base[s]) for s in SERVICES)
        cells.append(f"{fmt(total)} ({fmt_pct(pct_change(total, base_total))})")
        w(f"| {entry['variant']['label']} | " + " | ".join(cells) + " |")
    w("")


def markdown(results: dict) -> str:
    run = results["run"]
    base = baseline_of(results)
    variants = results["variants"]
    rps = float(run.get("project_rps", 100))
    out = []
    w = out.append

    w("# otel-muzzle benchmark results\n")
    w(f"Run `{run['run_id']}`: OpenTelemetry Demo {run['demo_version']} services, "
      f"{run['rate']} requests/s per service for {run['duration']} per variant "
      f"(after a {run['warmup_duration']} warmup that is not counted). "
      "Each request is one trace in each service it reaches.")
    forwarded = run.get("forward_export")
    rejected = run.get("forward_preflight_http")
    if forwarded:
        w("The same spans were also forwarded to OTLP_FORWARD_ENDPOINT as environments `muzzle-bench-<variant>`.\n")
    elif rejected:
        w(f"Nothing was forwarded: OTLP_FORWARD_ENDPOINT rejected the configured credentials "
          f"(HTTP {rejected}). All numbers come from the local collector.\n")
    else:
        w("Everything stayed on this machine.\n")

    # 1. Headline matrices
    w("## Spans per trace\n")
    w("Mean spans each service exports per trace, and the change against the stock demo images.\n")
    _matrix(w, variants, base, lambda st: st.spans_per_trace, lambda v: f"{v:.2f}")
    w("## Trace size\n")
    w("Mean encoded bytes of the spans each service exports per trace (OTLP protobuf, uncompressed, without the "
      "resource and scope that every export repeats), and the change against stock.\n")
    _matrix(w, variants, base, lambda st: st.trace_bytes, lambda v: f"{v:,.0f} B")
    w("## OTLP bytes sent per request\n")
    w("What the SDKs actually send, including the resource and scope of each export, per request.\n")
    _matrix(w, variants, base, lambda st: st.bytes_per_request, lambda v: f"{v:,.0f} B")

    # 2. Per-service detail
    w("## Per-service detail\n")
    w("*Verbose traces* kept their INTERNAL spans. *Rule mismatches* count traces where that disagrees with "
      "otel-muzzle's rule (the ratio selects the trace id, or the tracestate carries `muzzle=verbose`); it must be 0.\n")
    for s in (*SERVICES, CONTROL):
        title = f"{s} ({LANGUAGE[s]})" + (": control, never gets otel-muzzle" if s == CONTROL else "")
        w(f"### {title}\n")
        w("| Variant | Traces | Spans/trace | Server | Client | Internal | Trace bytes (mean / p95) | "
          "OTLP bytes/req | Verbose traces | Rule mismatches |")
        w("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for entry in variants:
            st = entry["stats"].get(s, ServiceStats())
            stock = entry["variant"]["images"] == "stock" or s == CONTROL
            w(f"| {entry['variant']['label']} | {st.traces:,} | {st.spans_per_trace:.2f} | "
              f"{st.kinds.get('server', 0):,} | {st.kinds.get('client', 0):,} | {st.kinds.get('internal', 0):,} | "
              f"{st.trace_bytes:,.0f} / {st.trace_bytes_p95:,.0f} | {st.bytes_per_request:,.0f} | "
              f"{st.verbose_share * 100:.1f}% | {'n/a' if stock else st.mismatches} |")
        w("")

    # 3. Ratio model check
    ratio_rows = [e for e in variants if e["variant"]["images"] != "stock" and e["variant"]["header_ratio"] == 0]
    floor = next((e for e in ratio_rows if float(e["variant"]["ratio"]) == 0.0), None)
    if floor:
        w("## Verbose ratio: modeled versus measured\n")
        w("A ratio `r` keeps every span of a share `r` of traces. If `s0` is spans per trace with the ratio at 0 "
          "and `sb` the stock value, the expected span reduction is `(1 - r) * (1 - s0/sb)`.\n")
        w("| Ratio | " + " | ".join(f"{s} modeled | {s} measured" for s in SERVICES) + " |")
        w("|---:|" + "---:|---:|" * len(SERVICES))
        for entry in ratio_rows:
            r = float(entry["variant"]["ratio"])
            cells = []
            for s in SERVICES:
                s0 = floor["stats"][s].spans_per_trace
                sb = base[s].spans_per_trace
                modeled = -(1 - r) * (1 - s0 / sb) * 100 if sb else None
                measured = pct_change(entry["stats"][s].spans_per_trace, sb)
                cells += [fmt_pct(modeled), fmt_pct(measured)]
            w(f"| {r * 100:.0f}% | " + " | ".join(cells) + " |")
        w("")

    # 3b. A verbose trace should cost what a stock trace costs
    full = next((e for e in ratio_rows if float(e["variant"]["ratio"]) == 1.0), None)
    if full:
        w("## What a verbose trace costs\n")
        w("At ratio 100% every span is kept, as in stock. otel-muzzle adds no attributes and no tracestate, so "
          "bytes per span should match stock within noise (span names, ids and timings vary).\n")
        w("| Service | Stock bytes/span | Ratio 100% bytes/span | Difference |")
        w("|---|---:|---:|---:|")
        for s in SERVICES:
            b, f = base[s], full["stats"][s]
            b_per = b.span_bytes / b.spans if b.spans else 0.0
            f_per = f.span_bytes / f.spans if f.spans else 0.0
            w(f"| {s} ({LANGUAGE[s]}) | {b_per:,.1f} | {f_per:,.1f} | {f_per - b_per:+,.1f} B "
              f"({fmt_pct(pct_change(f_per, b_per))}) |")
        w("")

    # 4. Projection
    w(f"## Projection at {rps:,.0f} requests/s per service\n")
    w("Linear extrapolation of the measured OTLP bytes and spans per request, per service, per day.\n")
    w("| Variant | " + " | ".join(f"{s} GB/day" for s in SERVICES) + " | Total GB/day | Saved GB/day | Spans/day (M) |")
    w("|---|" + "---:|" * (len(SERVICES) + 3))
    day = rps * 86400
    base_total = sum(base[s].bytes_per_request for s in SERVICES) * day / 1e9
    for entry in variants:
        st = entry["stats"]
        gb = [st[s].bytes_per_request * day / 1e9 for s in SERVICES]
        spans_m = sum(st[s].spans_per_request for s in SERVICES) * day / 1e6
        w(f"| {entry['variant']['label']} | " + " | ".join(f"{g:,.2f}" for g in gb)
          + f" | {sum(gb):,.2f} | {base_total - sum(gb):,.2f} | {spans_m:,.1f} |")
    w("")

    # 5. Load test and integrity
    w("## Load test and trace integrity\n")
    w("Latency is what k6 measured from inside the Docker network on this machine, so read it as a "
      "sanity check rather than a performance claim. Every request must produce exactly one exported "
      "server span per service, otherwise spans were lost rather than dropped. A dangling parent is a "
      "non-server span whose parent was never exported, which means a broken trace; reparenting keeps it "
      "at zero. Rule mismatches are summed over the three services.\n")
    w("| Variant | " + " | ".join(f"{s} ok / p50 / p95" for s in SERVICES)
      + " | Server spans = requests | Dangling parents | Rule mismatches |")
    w("|---|" + "---|" * len(SERVICES) + "---|---:|---:|")
    for entry in variants:
        st = entry["stats"]
        cells = []
        for s in SERVICES:
            lat = st[s].latency_ms
            cells.append(f"{(st[s].ok_rate or 0) * 100:.1f}% / {lat.get('p50') or 0:.1f} ms / {lat.get('p95') or 0:.1f} ms")
        mismatches = [f"{s} {st[s].server_spans}/{st[s].requests}"
                      for s in (*SERVICES, CONTROL) if st.get(s) and st[s].server_spans != st[s].requests]
        dangling = sum(v.dangling for v in st.values())
        rule = "n/a" if entry["variant"]["images"] == "stock" else sum(st[s].mismatches for s in SERVICES)
        w(f"| {entry['variant']['label']} | " + " | ".join(cells)
          + f" | {'yes' if not mismatches else 'NO: ' + ', '.join(mismatches)} | {dangling} | {rule} |")
    w("")

    w("## Variants\n")
    w("| Variant | Images | OTEL_MUZZLE_VERBOSE_RATIO | Requests sending `muzzle=verbose` |")
    w("|---|---|---:|---:|")
    for entry in variants:
        v = entry["variant"]
        stock = v["images"] == "stock"
        w(f"| {v['label']} | {'stock demo 3.1.0' if stock else 'demo 3.1.0 + otel-muzzle'} | "
          f"{'' if stock else v['ratio']} | {float(v['header_ratio']) * 100:.0f}% |")
    w("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="run directory (or a variant directory with --dump)")
    ap.add_argument("--dump", action="store_true", help="print span shapes for one variant directory")
    args = ap.parse_args(argv)

    if args.dump:
        dump(args.path)
        return 0

    results = build(args.path)
    flat = list(rows(results))
    with open(os.path.join(args.path, "results.csv"), "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
        writer.writeheader()
        writer.writerows(flat)
    with open(os.path.join(args.path, "results.json"), "w") as fh:
        json.dump({"run": results["run"], "rows": flat}, fh, indent=2)
    report = markdown(results)
    with open(os.path.join(args.path, "results.md"), "w") as fh:
        fh.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
