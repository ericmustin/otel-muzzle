#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Checks, inside Elastic, the traces examples/backends/elastic/demo.sh sent (stdlib only).

For every trace recorded in examples/backends/elastic/out/run.json it reads the spans back from Elasticsearch (ES|QL) and
checks:
  - the three services are present, and the trace is one connected tree: a single root, ruby-app's SERVER span,
    and every other span's parent among the trace's spans;
  - "full" traces kept INTERNAL spans in every service; "lean" traces have none;
  - SERVER and CLIENT spans per trace are the same with otel-muzzle off and on.
Then it asks Kibana's APM service map API for both environments over their run's time window and compares the
connections. It writes out/check.md and out/service_map_{off,on}.json, and exits 1 on any failure.

Needs KIBANA_URL and KIBANA_API_KEY (an API key that can read traces); ELASTICSEARCH_URL defaults to KIBANA_URL with
.kb. replaced by .es., as in Elastic Cloud.
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
SERVICES = ("ruby-app", "js-app", "python-app")


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # python.org's macOS builds start without CA certificates; use the system bundle rather than skip verification.
    if context.cert_store_stats()["x509_ca"] == 0 and Path("/etc/ssl/cert.pem").exists():
        context.load_verify_locations("/etc/ssl/cert.pem")
    return context


TLS = tls_context()


def call(url: str, body: dict | None = None, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"ApiKey {os.environ['KIBANA_API_KEY']}", "Content-Type": "application/json",
                 **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=90, context=TLS) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{urllib.parse.urlsplit(url).path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}")


def shifted(iso: str, seconds: int) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def spans_of(es: str, run: dict) -> dict[str, list[dict]]:
    """The run's spans in Elasticsearch, by trace id."""
    query = (
        "FROM traces-* "
        f'| WHERE resource.attributes.deployment.environment.name == "{run["environment"]}" '
        f'AND @timestamp >= "{shifted(run["start"], -60)}" AND @timestamp <= "{shifted(run["end"], 120)}" '
        "| KEEP trace_id, span_id, parent_span_id, kind, name, resource.attributes.service.name "
        "| LIMIT 10000"
    )
    result = call(f"{es}/_query?format=json", {"query": query})
    names = [c["name"] for c in result["columns"]]
    by_trace = defaultdict(list)
    for row in result["values"]:
        span = dict(zip(names, row))
        by_trace[span["trace_id"]].append(span)
    return by_trace


def check_trace(spans: list[dict], expect: str) -> list[str]:
    failures = []
    if not spans:
        return ["no spans in Elastic"]
    ids = {s["span_id"] for s in spans}
    services = {s["resource.attributes.service.name"] for s in spans}
    failures += [f"{svc} missing" for svc in SERVICES if svc not in services]
    roots = [s for s in spans if s["parent_span_id"] not in ids]
    if len(roots) != 1 or (roots[0]["resource.attributes.service.name"], roots[0]["kind"]) != ("ruby-app", "Server"):
        failures.append(f"{len(roots)} spans without an exported parent, expected only ruby-app's SERVER span")
    internal = Counter(s["resource.attributes.service.name"] for s in spans if s["kind"] == "Internal")
    if expect == "full":
        failures += [f"{svc} has no INTERNAL span" for svc in SERVICES if not internal[svc]]
    elif internal:
        failures.append(f"{sum(internal.values())} INTERNAL spans in a lean trace")
    return failures


def service_map(kibana: str, run: dict) -> tuple[dict, set[tuple[str, str]]]:
    query = urllib.parse.urlencode({"environment": run["environment"], "kuery": "",
                                    "start": shifted(run["start"], -60), "end": shifted(run["end"], 120)})
    result = call(f"{kibana}/internal/apm/service-map?{query}",
                  headers={"kbn-xsrf": "true", "x-elastic-internal-origin": "Kibana"})
    edges = set()
    for span in result.get("spans", []):
        destination = (span.get("destinationService") or {}).get("serviceName")
        edges.add((span["serviceName"], destination or f"{span['spanDestinationServiceResource']} (external)"))
    return result, edges


def main() -> int:
    kibana = os.environ["KIBANA_URL"].rstrip("/")
    es = os.environ.get("ELASTICSEARCH_URL", kibana.replace(".kb.", ".es.")).rstrip("/")
    runs = json.loads((OUT / "run.json").read_text())
    failures: list[str] = []
    rows, shape, notes = [], {}, []

    for variant in ("off", "on"):
        run = runs[variant]
        by_trace = spans_of(es, run)
        kinds: Counter = Counter()
        counts = []
        for t in run["traces"]:
            spans = by_trace.get(t["trace_id"], [])
            failures += [f"{variant} {t['trace_id']} ({t['expect']}): {f}" for f in check_trace(spans, t["expect"])]
            kinds.update(s["kind"] for s in spans)
            counts.append(len(spans))
        n = len(run["traces"])
        expects = Counter(t["expect"] for t in run["traces"])
        forced = sum(t["request"] != "plain" for t in run["traces"])
        shape[variant] = (kinds["Server"] / n, kinds["Client"] / n)
        notes.append(f"{run['environment']}: {forced} of the {n} requests sent `tracestate: muzzle=verbose`"
                     + (f"; the other {expects['full'] - forced} full traces are the ratio's picks." if variant == "on" else "."))
        rows.append(
            f"| {run['environment']} | {n} | {expects['full']} | {expects['lean']} | "
            f"{sum(counts) / n:.2f} | {kinds['Server'] / n:.2f} | {kinds['Client'] / n:.2f} | "
            f"{kinds['Internal'] / n:.2f} |"
        )
    if shape["off"] != shape["on"]:
        failures.append(f"SERVER/CLIENT spans per trace differ: off {shape['off']}, on {shape['on']}")

    maps = {}
    for variant in ("off", "on"):
        raw, edges = service_map(kibana, runs[variant])
        (OUT / f"service_map_{variant}.json").write_text(json.dumps(raw, indent=1) + "\n")
        maps[variant] = edges
    if maps["off"] != maps["on"]:
        failures.append(f"service maps differ: only off {sorted(maps['off'] - maps['on'])}, "
                        f"only on {sorted(maps['on'] - maps['off'])}")
    if not maps["off"]:
        failures.append("the service map has no connections")

    lines = [
        "# otel-muzzle in Elastic: what the data says",
        "",
        f"Runs: off {runs['off']['start']} – {runs['off']['end']}, on {runs['on']['start']} – {runs['on']['end']} (UTC).",
        "",
        "| Environment | Traces | Full | Lean | Spans per trace | SERVER | CLIENT | INTERNAL |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        *rows,
        "",
        *[f"- {note}" for note in notes],
        "",
        "Service map connections (Kibana `/internal/apm/service-map`), identical in both environments:"
        if maps["off"] == maps["on"] else "Service map connections DIFFER:",
        "",
        *[f"- {a} → {b}" for a, b in sorted(maps["off"] | maps["on"])],
        "",
        "Every trace: three services, one root (ruby-app's SERVER span), every parent present; full traces kept "
        "INTERNAL spans in every service, lean traces have none."
        if not failures else f"**{len(failures)} failures:**\n\n" + "\n".join(f"- {f}" for f in failures),
        "",
    ]
    (OUT / "check.md").write_text("\n".join(lines))
    print("\n".join(lines))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
