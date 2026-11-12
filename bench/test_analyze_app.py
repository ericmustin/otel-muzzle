# SPDX-License-Identifier: Apache-2.0
"""Tests for analyze_app.py. Run with `python3 -m unittest -q` from bench/."""

import json
import os
import struct
import tempfile
import unittest
from collections import Counter

import analyze
from analyze import Span
from analyze_app import (build, classify, dangling, is_verbose, markdown, outbound, process_of, request_type,
                         requests_in, spread_failures, spread_samples, stock_disagreement, violations)


def span(trace=1, id=1, parent=0, kind=2, name="GET /", process="puma", ua=None, state=""):
    """A span with ids built from small integers; parent=0 means no parent."""
    return Span(
        service="gitlab",
        scope="test",
        trace_id=trace.to_bytes(16, "big"),
        span_id=id.to_bytes(8, "big"),
        parent_id=parent.to_bytes(8, "big") if parent else b"",
        name=name,
        kind=kind,
        trace_state=state,
        size=100,
        attributes={"user_agent.original": ua} if ua else {},
        resource={"process.command": process},
    )


class Classify(unittest.TestCase):
    def test_k6_root_is_a_request(self):
        t = [span(ua="muzzle-bench-k6"), span(id=2, parent=1, kind=3)]
        self.assertEqual(classify(t), "request")

    def test_other_root_is_background(self):
        self.assertEqual(classify([span(ua="gitlab-workhorse"), span(id=2, parent=1, kind=3)]), "background")
        self.assertEqual(classify([span(kind=5, process="sidekiq")]), "background")

    def test_trace_without_a_root_is_cut(self):
        self.assertEqual(classify([span(id=2, parent=1, kind=1), span(id=3, parent=2, kind=3)]), "cut")

    def test_server_span_under_a_remote_parent_is_a_root(self):
        t = [span(id=1, parent=99, ua="muzzle-bench-k6", state="muzzle=verbose"), span(id=2, parent=1, kind=1)]
        self.assertEqual(classify(t), "request")
        self.assertEqual(dangling(t), 0)


class Rule(unittest.TestCase):
    def test_internal_under_a_local_parent_violates(self):
        self.assertEqual(violations([span(), span(id=2, parent=1, kind=1)]), 1)

    def test_internal_root_and_internal_under_a_remote_parent_do_not(self):
        self.assertEqual(violations([span(kind=1)]), 0)
        self.assertEqual(violations([span(id=2, parent=99, kind=1)]), 0)

    def test_parent_in_another_process_is_remote(self):
        self.assertEqual(violations([span(), span(id=2, parent=1, kind=1, process="sidekiq")]), 0)

    def test_verbose_trace_may_keep_internal_spans(self):
        t = [span(state="muzzle=verbose"), span(id=2, parent=1, kind=1, state="muzzle=verbose")]
        self.assertTrue(is_verbose(t[0].trace_id, t, 0.0))

    def test_trace_with_no_internal_spans_never_violates(self):
        self.assertEqual(violations([span(kind=3)]), 0)

    def test_ratio_uses_trace_id_bytes_12_to_15(self):
        t = [span()]
        self.assertTrue(is_verbose(t[0].trace_id, t, 1.0))
        self.assertFalse(is_verbose(t[0].trace_id, t, 0.0))


class Process(unittest.TestCase):
    def test_process_splits_puma_and_sidekiq(self):
        self.assertEqual(process_of(span(process="/opt/gitlab/embedded/bin/puma")), "web")
        self.assertEqual(process_of(span(process="/opt/gitlab/embedded/lib/ruby/gems/3.3.0/bin/sidekiq")), "sidekiq")

    def test_rake_and_rails_runner_are_neither(self):
        self.assertEqual(process_of(span(process="/opt/gitlab/embedded/bin/rake")), "other")
        self.assertEqual(process_of(span(process="bin/rails")), "other")


class RequestType(unittest.TestCase):
    def test_rails_requests_are_named_by_controller_and_action(self):
        root = span(name="GET /*namespace_id/:id", ua="muzzle-bench-k6")
        root.attributes.update({"http.route": "/*namespace_id/:id", "code.namespace": "ProjectsController",
                                "code.function": "show"})
        self.assertEqual(request_type([root]), "ProjectsController#show")

    def test_api_requests_are_named_by_their_root_span(self):
        root = span(name="HTTP POST /api/v4/projects/:id/issues", ua="muzzle-bench-k6")
        root.attributes["http.route"] = "/api/v4/projects/:id/issues"
        self.assertEqual(request_type([root]), "HTTP POST /api/v4/projects/:id/issues")


class RequestsIn(unittest.TestCase):
    def test_two_k6_requests_in_one_trace_are_counted_apart(self):
        # Workhorse passes a forced traceparent to the /authorize preflight and to the request itself.
        t = [span(id=1, parent=99, name="authorize", ua="muzzle-bench-k6"), span(id=2, parent=1, kind=3),
             span(id=3, parent=99, name="commit", ua="muzzle-bench-k6"), span(id=4, parent=3, kind=4),
             span(id=5, parent=4, kind=3)]
        found = requests_in(t)
        self.assertEqual([request_type(r) for r in found], ["authorize", "commit"])
        self.assertEqual([len(r) for r in found], [2, 3])


class StockAgreement(unittest.TestCase):
    def test_stock_runs_that_disagree_on_the_headline_fail(self):
        ref = {"spans_per_request": 208.29, "bytes_per_request": 59_500}
        burst = {"spans_per_request": 374.37, "bytes_per_request": 151_680}
        self.assertEqual(len(stock_disagreement(ref, burst)), 2)

    def test_stock_runs_within_the_bound_pass(self):
        ref = {"spans_per_request": 184.66, "bytes_per_request": 52_280}
        self.assertEqual(stock_disagreement(ref, {"spans_per_request": 184.91, "bytes_per_request": 52_380}), [])


class Outbound(unittest.TestCase):
    def client(self, peer):
        s = span(kind=3, name="HTTP GET", process="sidekiq")
        s.attributes.update({"http.method": "GET", "net.peer.name": peer})
        return s

    def test_calls_to_other_machines_are_outbound(self):
        self.assertEqual(outbound([self.client("35.190.167.255"), self.client("events.gitlab.net")]),
                         {"35.190.167.255": 1, "events.gitlab.net": 1})

    def test_calls_inside_the_compose_network_are_not(self):
        self.assertEqual(outbound([self.client("gitlab"), self.client("localhost"), self.client("172.18.0.3"),
                                   self.client("127.0.0.1")]), {})

    def test_redis_and_database_clients_are_not_http(self):
        s = span(kind=3, name="GET", process="sidekiq")
        s.attributes.update({"db.system": "redis", "net.peer.name": "redis.example.com"})
        self.assertEqual(outbound([s]), {})


class Spread(unittest.TestCase):
    def test_within_baseline_spread_passes(self):
        self.assertEqual(spread_failures({"a|client": [10] * 30}, {"a|client": [12] * 30}, {"a|client": [11] * 30}), [])

    def test_shift_beyond_spread_and_noise_fails(self):
        failures = spread_failures({"a|client": [10] * 30}, {"a|client": [10] * 30}, {"a|client": [13] * 30})
        self.assertEqual(failures, ["a|client: 13.00 vs baseline 10.00 (tolerance 0.00)"])

    def test_identical_baselines_still_allow_sampling_noise(self):
        # Mean 10.33 against 10: only the three-standard-error term covers the difference.
        noisy = [9, 11, 11] * 10
        self.assertEqual(spread_failures({"a|client": [10] * 30}, {"a|client": [10] * 30}, {"a|client": noisy}), [])

    def test_keys_with_too_few_traces_are_skipped(self):
        self.assertEqual(spread_failures({"a|client": [1] * 5}, {"a|client": [1] * 5}, {"a|client": [9] * 5}), [])


class SpreadSamples(unittest.TestCase):
    """Per request trace: (request type, span counts by kind, verbose)."""

    STOCK_INTERNAL = {"create": 50.0, "view": 100.0}
    STOCK = [("create", Counter({4: 3, 1: 50}), True)] * 40 + [("view", Counter({3: 5, 1: 100}), True)] * 40

    def test_a_different_request_mix_is_not_a_change(self):
        # Fewer creates, which carry the PRODUCER spans, and a verbose share with a different mix of types.
        stock = spread_samples(self.STOCK, self.STOCK_INTERNAL)
        muzzle = spread_samples(
            [("create", Counter({4: 3}), False)] * 20 + [("view", Counter({3: 5}), False)] * 60
            + [("create", Counter({4: 3, 1: 50}), True)] * 5 + [("view", Counter({3: 5, 1: 100}), True)] * 25,
            self.STOCK_INTERNAL,
        )
        self.assertEqual(spread_failures(stock, stock, muzzle), [])

    def test_verbose_traces_that_lost_internal_spans_fail(self):
        stock = spread_samples(self.STOCK, self.STOCK_INTERNAL)
        muzzle = spread_samples([("view", Counter({3: 5, 1: 10}), True)] * 30, self.STOCK_INTERNAL)
        self.assertEqual(len(spread_failures(stock, stock, muzzle)), 1)


# A test-only OTLP encoder: the inverse of analyze._fields and analyze.read_records, for the fields analyze._span reads.


def _enc_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _enc_len(number: int, payload: bytes) -> bytes:
    return _enc_varint(number << 3 | 2) + _enc_varint(len(payload)) + payload


def _enc_attributes(number: int, attributes: dict) -> bytes:
    return b"".join(_enc_len(number, _enc_len(1, k.encode()) + _enc_len(2, _enc_len(1, v.encode())))
                    for k, v in attributes.items())


def _enc_span(s: dict) -> bytes:
    return (_enc_len(1, s["trace_id"]) + _enc_len(2, s["span_id"]) + _enc_len(3, s.get("trace_state", "").encode())
            + _enc_len(4, s.get("parent_id", b"")) + _enc_len(5, s["name"].encode())
            + _enc_varint(6 << 3) + _enc_varint(s["kind"]) + _enc_attributes(9, s.get("attributes", {})))


def otlp_record(service: str, spans: list[dict], scope: str = "test", process: str = "puma") -> bytes:
    """One length-prefixed collector record: an export request with a single ResourceSpans."""
    resource = _enc_attributes(1, {"service.name": service, "process.command": process})
    scope_spans = _enc_len(1, _enc_len(1, scope.encode())) + b"".join(_enc_len(2, _enc_span(s)) for s in spans)
    request = _enc_len(1, _enc_len(1, resource) + _enc_len(2, scope_spans))
    return struct.pack(">I", len(request)) + request


def ids(trace: int, span: int = 0, parent: int = 0) -> dict:
    """Ids from small integers. Trace-id bytes 12-15 are 0xFFFFFFFF, so no ratio below 1 selects the trace."""
    return {"trace_id": b"\0" * 8 + trace.to_bytes(4, "big") + b"\xff" * 4,
            "span_id": span.to_bytes(8, "big"), "parent_id": parent.to_bytes(8, "big") if parent else b""}


def request_trace(n: int, internal: bool) -> list[dict]:
    """A k6 request: SERVER, then INTERNAL, then CLIENT; without the INTERNAL span the CLIENT is the SERVER's child."""
    server = {**ids(n, 1), "name": "GET /x", "kind": 2, "attributes": {
        "user_agent.original": "muzzle-bench-k6", "code.namespace": "XController", "code.function": "show"}}
    client = {**ids(n, 3, 2 if internal else 1), "name": "SELECT", "kind": 3}
    middle = [{**ids(n, 2, 1), "name": "render", "kind": 1}] if internal else []
    return [server, *middle, client]


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = self.tmp.name

    def write_run(self, variants: list[tuple[str, str, float]]) -> None:
        """variants: (name, images, ratio). Muzzle variants export no INTERNAL span."""
        meta = []
        for name, images, ratio in variants:
            directory = os.path.join(self.run_dir, name)
            os.makedirs(directory)
            muzzle = images != "stock"
            for phase in ("measure", "warmup"):
                with open(os.path.join(directory, phase + ".pb"), "wb") as fh:
                    for n in range(1, 26):
                        fh.write(otlp_record("gitlab", request_trace(n, internal=not muzzle)))
            with open(os.path.join(directory, "k6-measure.json"), "w") as fh:
                json.dump({"http_reqs": 25, "checks_rate": 1}, fh)
            meta.append({"name": name, "label": name, "images": images, "ratio": str(ratio)})
        with open(os.path.join(self.run_dir, "run.json"), "w") as fh:
            json.dump({"run_id": "test", "gitlab_version": "0", "rate": 4, "duration": "10s",
                       "variants": meta}, fh)

    def test_encoder_round_trips(self):
        trace = request_trace(7, internal=True)
        path = os.path.join(self.run_dir, "x.pb")
        with open(path, "wb") as fh:
            fh.write(otlp_record("svc", trace, scope="lib", process="sidekiq"))
        self.assertEqual(len(list(analyze.read_records(path))), 1)
        spans, by_service = analyze.parse(path)
        self.assertEqual([(s.trace_id, s.span_id, s.parent_id, s.name, s.kind) for s in spans],
                         [(t["trace_id"], t["span_id"], t["parent_id"], t["name"], t["kind"]) for t in trace])
        self.assertEqual(spans[0].attributes["code.function"], "show")
        self.assertEqual((spans[0].service, spans[0].scope, spans[0].resource["process.command"]),
                         ("svc", "lib", "sidekiq"))
        self.assertEqual(list(by_service), ["svc"])
        self.assertEqual(spans[0].size + spans[1].size + spans[2].size,
                         sum(1 + analyze._varint_size(len(_enc_span(t))) + len(_enc_span(t)) for t in trace))

    def test_build_and_markdown_over_written_runs(self):
        self.write_run([("baseline", "stock", 0), ("baseline-2", "stock", 0), ("muzzle-10", "muzzle", 0.1)])
        results = build(self.run_dir)
        self.assertEqual(len(results["variants"]), 3)
        muzzle = next(v for v in results["variants"] if v["name"] == "muzzle-10")
        self.assertTrue(muzzle["checks"]["passed"], muzzle["checks"])
        self.assertEqual(muzzle["spans"], 50)
        text = markdown(results)
        for name in ("baseline", "baseline-2", "muzzle-10"):
            self.assertRegex(text, rf"(?m)^\| {name} \| 25 \|")
        self.assertIn("iterations/s", text)
        self.assertIn("Request traces by type", text)

    def test_markdown_with_only_stock_variants(self):
        self.write_run([("baseline", "stock", 0), ("baseline-2", "stock", 0)])
        text = markdown(build(self.run_dir))
        self.assertRegex(text, r"(?m)^\| baseline-2 \| 25 \|")
        self.assertNotIn("Request traces by type", text)

    def test_other_column_only_when_populated(self):
        self.write_run([("baseline", "stock", 0), ("baseline-2", "stock", 0)])
        self.assertNotIn("| Other |", markdown(build(self.run_dir)))


if __name__ == "__main__":
    unittest.main()
