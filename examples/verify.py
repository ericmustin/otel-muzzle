#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""End-to-end verifier for the otel-muzzle sample apps (stdlib only).

Sends four requests to the entry app and checks the trace each one produced in the collector's file exporter
output (one OTLP JSON export batch per line):

  1. quiet      sampled traceparent whose trace id the ratio does not select   lean
  2. header     the same, plus tracestate muzzle=verbose                     full
  3. ratio      sampled traceparent whose trace id the ratio selects            full
  4. minted     no traceparent; the entry app mints the trace id                lean or full, from the id

With POLICY_FILE set (the apps run in policy mode with the same file, make e2e POLICY=1), a trace is full when
its randomness, trace-id bytes 9-15, is at or above the policy's rejection threshold, and a fifth request is sent:

  5. rv         a trace id the policy selects, plus tracestate ot=rv:00000000000000    lean

The explicit randomness value replaces the trace id in every service, so the trace is lean.

With ROOTS_DROPPED=1 (the apps run OTEL_TRACES_SAMPLER=parentbased_traceidratio, OTEL_TRACES_SAMPLER_ARG=0),
scenario 4 must export nothing in any service: otel-muzzle wraps the configured sampler, and its DROP wins.

lean: no INTERNAL spans; every CLIENT span hangs off its own service's SERVER span (it was started under a
dropped INTERNAL span, so this is the reparenting at work). full: every service kept its INTERNAL span.
Both: one root, every parent exported (the trace is connected), every downstream SERVER span under a CLIENT
span of another service, and every service in SERVICES present.

Environment:
  ENTRY_URL    entry app, e.g. http://ruby-app:4567
  ENTRY_PATH   /hello (one service) or /chain (ruby -> js -> python -> js)
  SERVICES     comma-separated service names that must appear, e.g. ruby-app or ruby-app,js-app,python-app
  APP_URLS     comma-separated base URLs whose /health must answer before the run
  SPANS_FILE   collector output, e.g. /out/spans.jsonl
  RATIO        the apps' OTEL_MUZZLE_VERBOSE_RATIO (default 0.1)
  POLICY_FILE  the apps' OTEL_MUZZLE_POLICY_FILE, when they run in policy mode; RATIO is then unused
  ROOTS_DROPPED 1 when the apps' configured sampler drops new roots (see above)

Usage:
  verify.py              run against the apps (exit 1 on any failure)
  verify.py --selftest   check the parser and assertions on hand-written OTLP lines (no Docker needed)
"""

import json
import math
import os
import secrets
import sys
import time
import urllib.request
from dataclasses import dataclass, field

KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent: str | None  # None for a root
    name: str
    kind: str
    service: str
    trace_state: str = ""
    start: int = 0
    attrs: dict = field(default_factory=dict)


# --- the ratio rule, shared with the packages (testdata/ratio_vectors.json) --------------------------


def ratio_selects(trace_id, ratio):
    """Trace-id bytes 12-15 (the last 8 hex characters), as a big-endian uint32, below floor(ratio * 2**32)."""
    return int(trace_id[24:32], 16) < math.floor(ratio * 2.0**32)


def trace_id_where(selected, selects):
    """A random trace id the rule does (or does not) select."""
    while True:
        trace_id = secrets.token_hex(16)
        if selects(trace_id) == selected:
            return trace_id


# --- the policy rule, shared with the packages (testdata/policy_threshold_vectors.json) --------------

MAX_THRESHOLD = 1 << 56


def policy_threshold(percentage, precision=4):
    """The rejection threshold of a span sampling policy: 2**56 - round(percentage / 100 * 2**56), rounded half up
    to `precision` hexadecimal digits counted after the leading f digits, 14 at most."""
    probability = percentage / 100
    if probability <= 0:
        return MAX_THRESHOLD
    if probability >= 1:
        return 0
    scaled = probability * MAX_THRESHOLD
    whole = math.floor(scaled)
    threshold = MAX_THRESHOLD - (whole + (1 if scaled - whole >= 0.5 else 0))
    shift = 4 * (14 - min(14, precision + (-math.frexp(probability)[1]) // 4))
    if shift > 0:
        threshold = ((threshold + (1 << (shift - 1))) >> shift) << shift
    return threshold


def policy_selects(trace_id, threshold):
    """Trace-id bytes 9-15 (the last 14 hex characters) at or above the rejection threshold."""
    return int(trace_id[18:32], 16) >= threshold


def policy_file_threshold(text):
    """The threshold of a policy file with one span sampling policy, such as examples/policies/muzzle.json."""
    (policy,) = json.loads(text)["policies"]
    sampling = (policy.get("spanSampling") or policy["span_sampling"])["sampling"]
    precision = sampling.get("samplingPrecision", sampling.get("sampling_precision", 4))
    return policy_threshold(float(sampling["percentage"]), int(precision))


# --- parsing (pure) ---------------------------------------------------------------------------------


def _attributes(key_values):
    out = {}
    for kv in key_values or []:
        value = kv.get("value", {})
        out[kv["key"]] = next(iter(value.values()), None) if value else None
    return out


def parse_batch(line):
    """One file-exporter line (an OTLP export batch) -> list of Span."""
    spans = []
    for resource_spans in json.loads(line).get("resourceSpans", []):
        service = _attributes(resource_spans.get("resource", {}).get("attributes")).get("service.name", "?")
        for scope_spans in resource_spans.get("scopeSpans", []):
            for s in scope_spans.get("spans", []):
                spans.append(Span(
                    trace_id=s["traceId"].lower(),
                    span_id=s["spanId"].lower(),
                    parent=s.get("parentSpanId", "").lower() or None,
                    name=s.get("name", ""),
                    kind=KINDS.get(s.get("kind", 0), str(s.get("kind"))),
                    service=service,
                    trace_state=s.get("traceState", ""),
                    start=int(s.get("startTimeUnixNano", 0)),
                    attrs=_attributes(s.get("attributes")),
                ))
    return spans


def spans_of(text, trace_id):
    """Every span of one trace in the spans file. The last line may be mid-write, so it is skipped."""
    spans = []
    for line in text.split("\n")[:-1]:
        if not line.strip():
            continue
        try:
            spans += [s for s in parse_batch(line) if s.trace_id == trace_id]
        except (ValueError, KeyError):
            continue
    return spans


# --- assertions (pure) ------------------------------------------------------------------------------


def _describe(span):
    return f"{span.service} {span.kind} {span.name!r} [{span.span_id}]"


def check_trace(spans, services, expect, external_parent=None):
    """Failed assertions for one trace. expect is "lean" or "full"."""
    if not spans:
        return ["no spans found for this trace"]
    failures = []
    by_id = {s.span_id: s for s in spans}
    present = {s.service for s in spans}
    failures += [f"{svc}: no spans in this trace" for svc in services if svc not in present]

    roots = [s for s in spans if s.parent in (None, external_parent)]
    if len(roots) != 1 or (roots[0].service, roots[0].kind) != (services[0], "SERVER"):
        found = ", ".join(map(_describe, roots)) or "none"
        failures.append(f"expected one root, the {services[0]} SERVER span; found {len(roots)}: {found}")
    for s in spans:
        if s.parent in (None, external_parent):
            continue
        parent = by_id.get(s.parent)
        if parent is None:
            failures.append(f"{_describe(s)}: parent {s.parent} is not an exported span of this trace")
        elif s.kind == "SERVER" and (parent.kind != "CLIENT" or parent.service == s.service):
            failures.append(f"{_describe(s)}: parent is {_describe(parent)}, expected a CLIENT span of another service")

    for svc in services:
        mine = [s for s in spans if s.service == svc]
        if mine and not any(s.kind == "CLIENT" for s in mine):
            failures.append(f"{svc}: no CLIENT span (each app makes at least one call under its INTERNAL span)")
        if expect == "full" and mine and not any(s.kind == "INTERNAL" for s in mine):
            failures.append(f"{svc}: no INTERNAL span, but this trace is verbose")

    if expect == "lean":
        for s in spans:
            if s.kind == "INTERNAL":
                failures.append(f"{_describe(s)}: INTERNAL span should have been dropped")
            elif s.kind == "CLIENT":
                parent = by_id.get(s.parent)
                if parent is not None and (parent.service, parent.kind) != (s.service, "SERVER"):
                    failures.append(f"{_describe(s)}: parent is {_describe(parent)}, expected its service's SERVER span")
    return failures


def check_header_propagated(spans, services, member="muzzle=verbose"):
    """Scenarios 2 and 5: downstream SERVER spans received the tracestate member."""
    failures = []
    for s in spans:
        if s.kind == "SERVER" and s.service != services[0] and s.service in services:
            members = [m.strip() for m in s.trace_state.split(",")]
            if member not in members:
                failures.append(f"{_describe(s)}: traceState {s.trace_state!r} lacks {member}")
    return failures


def render_tree(spans, external_parent=None):
    ids = {s.span_id for s in spans}
    children = {}
    for s in spans:
        children.setdefault(s.parent if s.parent in ids else None, []).append(s)
    lines = []

    def walk(span, depth):
        note = ""
        if span.parent and span.parent not in ids:
            note = "  (parent = traceparent)" if span.parent == external_parent else f"  (MISSING parent {span.parent})"
        lines.append(f"{span.service:<11} {span.kind:<8} {'  ' * depth}{span.name}{note}")
        for child in sorted(children.get(span.span_id, []), key=lambda c: c.start):
            walk(child, depth + 1)

    for root in sorted(children.get(None, []), key=lambda r: r.start):
        walk(root, 0)
    return lines


# --- running against the apps (I/O) -----------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    headers: dict
    expect: str | None  # "lean", "full", "none", or None to decide from the trace id the entry app reports
    trace_id: str | None = None
    parent_id: str | None = None


def scenarios(selects, any_selected=True, roots_dropped=False, policy=False):
    """selects(trace_id) is the rule in effect; any_selected is False when it selects no trace id at all."""

    def sampled(name, selected, expect, tracestate=None):
        trace_id, parent_id = trace_id_where(selected, selects), secrets.token_hex(8)
        headers = {"traceparent": f"00-{trace_id}-{parent_id}-01"}
        if tracestate:
            headers["tracestate"] = tracestate
        return Scenario(name, headers, expect, trace_id, parent_id)

    out = [sampled("1 quiet trace id", False, "lean"),
           sampled("2 tracestate muzzle=verbose", False, "full", "muzzle=verbose")]
    if any_selected:
        out.append(sampled("3 trace id the rule selects", True, "full"))
    if roots_dropped:
        out.append(Scenario("4 no traceparent, root dropped", {}, "none"))
    else:
        out.append(Scenario("4 no traceparent", {}, None))
    if policy and any_selected:
        out.append(sampled("5 tracestate ot=rv:00000000000000", True, "lean", "ot=rv:00000000000000"))
    return out


def get_json(url, headers=None, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as response:
        return json.load(response)


def wait_for_apps(urls, timeout=180):
    deadline = time.monotonic() + timeout
    for url in urls:
        while True:
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=3) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit(f"{url}/health did not answer within {timeout}s")
            time.sleep(0.5)


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def wait_for_trace(path, trace_id, services, timeout=20.0, settle=1.5):
    """Poll until every service's SERVER span is in and nothing new arrived for `settle` seconds."""
    deadline = time.monotonic() + timeout
    count, changed = -1, time.monotonic()
    while True:
        spans = spans_of(read_text(path), trace_id)
        now = time.monotonic()
        if len(spans) != count:
            count, changed = len(spans), now
        servers = {s.service for s in spans if s.kind == "SERVER"}
        if (servers >= set(services) and now - changed >= settle) or now >= deadline:
            return spans
        time.sleep(0.25)


def run_scenario(env, sc):
    print(f"\n--- scenario {sc.name} ---")
    url = f"{env['entry']}{env['path']}"
    try:
        body = get_json(url, sc.headers)
    except (OSError, ValueError) as e:
        print(f"FAIL\n  - GET {url} failed: {e}")
        return [f"GET {url} failed: {e}"], None, []
    trace_id = body.get("trace_id") if isinstance(body, dict) else None
    if not trace_id:
        print(f"FAIL\n  - the response carries no trace_id: {body!r}")
        return [f"the response carries no trace_id: {body!r}"], None, []

    failures = []
    if sc.trace_id and trace_id != sc.trace_id:
        failures.append(f"entry reported trace {trace_id}, expected the traceparent's {sc.trace_id}")
    expect = sc.expect or ("full" if env["selects"](trace_id) else "lean")
    if expect == "none":
        # Longer than every SDK's 5 s batch delay; scenarios 1-3 already showed the pipeline delivers.
        time.sleep(8)
        spans = spans_of(read_text(env["spans"]), trace_id)
        if spans:
            failures.append(f"the configured sampler drops new roots, yet {len(spans)} spans were exported by "
                            f"{sorted({s.service for s in spans})}")
        print(f"trace {trace_id}: {len(spans)} spans, expected none")
        print("PASS" if not failures else "FAIL\n" + "\n".join(f"  - {f}" for f in failures))
        return failures, trace_id, spans
    spans = wait_for_trace(env["spans"], trace_id, env["services"])
    failures += check_trace(spans, env["services"], expect, sc.parent_id)
    if "tracestate" in sc.headers:
        failures += check_header_propagated(spans, env["services"], sc.headers["tracestate"])
    print(f"trace {trace_id}: {len(spans)} spans, expected {expect}")
    for line in render_tree(spans, sc.parent_id):
        print(f"  {line}")
    print("PASS" if not failures else "FAIL\n" + "\n".join(f"  - {f}" for f in failures))
    return failures, trace_id, spans


def main():
    env = {
        "entry": os.environ["ENTRY_URL"],
        "path": os.environ.get("ENTRY_PATH", "/chain"),
        "services": [s.strip() for s in os.environ["SERVICES"].split(",") if s.strip()],
        "spans": os.environ["SPANS_FILE"],
        "roots_dropped": os.environ.get("ROOTS_DROPPED") == "1",
    }
    policy_file = os.environ.get("POLICY_FILE", "").strip()
    if policy_file:
        with open(policy_file, encoding="utf-8") as f:
            threshold = policy_file_threshold(f.read())
        env["selects"], any_selected = (lambda trace_id: policy_selects(trace_id, threshold)), threshold < MAX_THRESHOLD
        print(f"policy mode: {policy_file}, rejection threshold {threshold:014x}")
    else:
        ratio = float(os.environ.get("RATIO", "0.1"))
        env["selects"], any_selected = (lambda trace_id: ratio_selects(trace_id, ratio)), ratio > 0
    wait_for_apps([u.strip() for u in os.environ.get("APP_URLS", env["entry"]).split(",") if u.strip()])
    plan = scenarios(env["selects"], any_selected, env["roots_dropped"], bool(policy_file))
    results = [(sc, *run_scenario(env, sc)) for sc in plan]

    print("\n=== summary ===")
    for sc, failures, trace_id, spans in results:
        verdict = "PASS" if not failures else f"FAIL ({len(failures)})"
        print(f"{sc.name:<34} {trace_id or '-':<32} {len(spans):>3} spans  {verdict}")
    failed = sum(bool(failures) for _, failures, _, _ in results)
    print(f"\n{failed} of {len(results)} scenarios FAILED" if failed else "\nALL PASS")
    return 1 if failed else 0


# --- selftest ---------------------------------------------------------------------------------------

_T = "4bf92f3577b34da6a3ce929dffffffff"
_EXTERNAL = "00f067aa0ba902b7"


def _span(span_id, parent, name, kind, trace_state=""):
    out = {"traceId": _T, "spanId": span_id, "name": name, "kind": kind, "startTimeUnixNano": "1"}
    if parent:
        out["parentSpanId"] = parent
    if trace_state:
        out["traceState"] = trace_state
    return out


def _line(service, *spans):
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}
    return json.dumps({"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": {"name": "s"}, "spans": list(spans)}]}]})


def _chain(verbose=False, reparent=True, drop=True):
    """ruby SERVER -> [work] -> ruby CLIENT -> js SERVER -> [work] -> js CLIENT -> python SERVER -> [work] -> python CLIENT."""
    lines = []
    parent = _EXTERNAL
    for i, svc in enumerate(("ruby-app", "js-app", "python-app")):
        server, work, client = f"{i}000000000000001", f"{i}000000000000002", f"{i}000000000000003"
        state = "muzzle=verbose" if verbose and i else ""
        spans = [_span(server, parent, "GET", 2, state)]
        if not drop:
            spans.append(_span(work, server, "work", 1))
        client_parent = server if reparent and drop else work
        spans.append(_span(client, client_parent, "SELECT", 3))
        lines.append(_line(svc, *spans))
        parent = client
    return "\n".join(lines) + "\n"


def selftest():
    services = ["ruby-app", "js-app", "python-app"]
    cases = [
        ("lean chain passes", _chain(), "lean", 0),
        ("full chain passes", _chain(drop=False), "full", 0),
        ("unreparented children are caught", _chain(reparent=False), "lean", 3),
        ("INTERNAL spans in a lean trace are caught", _chain(drop=False), "lean", 6),
        ("missing INTERNAL spans in a full trace are caught", _chain(), "full", 3),
    ]
    ok = True
    for title, text, expect, want in cases:
        got = check_trace(spans_of(text, _T), services, expect, _EXTERNAL)
        passed = len(got) == want
        ok &= passed
        print(f"{'ok  ' if passed else 'FAIL'} {title}: {len(got)} failures (want {want})" + ("" if passed else f" {got}"))

    one = spans_of(_chain(), _T)
    ruby_only = [s for s in one if s.service == "ruby-app"]
    got = check_trace(ruby_only, ["ruby-app"], "lean", _EXTERNAL)
    ok &= not got
    print(f"{'ok  ' if not got else 'FAIL'} a single service passes on its own {got or ''}")

    got = check_header_propagated(spans_of(_chain(verbose=True), _T), services)
    ok &= not got
    print(f"{'ok  ' if not got else 'FAIL'} propagated tracestate is accepted {got or ''}")
    got = check_header_propagated(spans_of(_chain(), _T), services)
    ok &= len(got) == 2
    print(f"{'ok  ' if len(got) == 2 else 'FAIL'} missing tracestate is caught: {len(got)} failures (want 2)")

    torn = _chain() + '{"resourceSpans": ['
    ok &= len(spans_of(torn, _T)) == 6
    print(f"{'ok  ' if len(spans_of(torn, _T)) == 6 else 'FAIL'} a half-written last line is skipped")

    vectors = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "testdata", "ratio_vectors.json")
    if os.path.exists(vectors):
        with open(vectors, encoding="utf-8") as f:
            wrong = [c for c in json.load(f)["cases"] if ratio_selects(c["trace_id"], c["ratio"]) != c["verbose"]]
        ok &= not wrong
        print(f"{'ok  ' if not wrong else 'FAIL'} ratio rule matches testdata/ratio_vectors.json {wrong[:3] or ''}")
    got = check_header_propagated(spans_of(_chain(verbose=True), _T), services, "ot=rv:00000000000000")
    ok &= len(got) == 2
    print(f"{'ok  ' if len(got) == 2 else 'FAIL'} another tracestate member is looked for by name: {len(got)} failures (want 2)")

    here = os.path.dirname(os.path.abspath(__file__))
    vectors = os.path.join(here, "..", "testdata", "policy_threshold_vectors.json")
    if os.path.exists(vectors):
        with open(vectors, encoding="utf-8") as f:
            wrong = [c for c in json.load(f)["cases"]
                     if f"{policy_threshold(c['percentage'], c['precision']):014x}" != c["threshold"]]
        ok &= not wrong
        print(f"{'ok  ' if not wrong else 'FAIL'} policy threshold matches testdata/policy_threshold_vectors.json {wrong[:3] or ''}")
    example = os.path.join(here, "policies", "muzzle.json")
    if os.path.exists(example):
        with open(example, encoding="utf-8") as f:
            threshold = policy_file_threshold(f.read())
        passed = (threshold == 0xE6660000000000 and policy_selects("4bf92f3577b34da6a3e6660000000000", threshold)
                  and not policy_selects("4bf92f3577b34da6a3e665ffffffffff", threshold))
        ok &= passed
        print(f"{'ok  ' if passed else 'FAIL'} the example policy selects trace ids at or above e6660000000000")
    names = [sc.name[0] for sc in scenarios(lambda trace_id: policy_selects(trace_id, 0xE666 << 40), policy=True)]
    plain = [sc.name[0] for sc in scenarios(lambda trace_id: ratio_selects(trace_id, 0.1))]
    passed = names == ["1", "2", "3", "4", "5"] and plain == ["1", "2", "3", "4"]
    ok &= passed
    print(f"{'ok  ' if passed else 'FAIL'} policy mode adds the rv scenario: {names} and {plain}")
    print("\nselftest " + ("passed" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else main())
