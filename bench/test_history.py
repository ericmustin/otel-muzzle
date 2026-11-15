# SPDX-License-Identifier: Apache-2.0
"""Tests for history.py. Run with `python3 -m unittest -q` from bench/."""

import unittest

from history import entries


def demo_row(variant, service, spans, trace_bytes, otlp):
    return {"variant": variant, "service": service, "spans_per_trace": spans, "trace_bytes_mean": trace_bytes,
            "otlp_bytes_per_request": otlp}


def gitlab_variant(name, spans, otlp, types):
    return {"name": name, "spans_per_request": spans, "bytes_per_request": otlp, "request_types": types}


class Demo(unittest.TestCase):
    results = {
        "passed": True,
        "rows": [
            demo_row("baseline", "email", 4.0, 712.0, 717.0),
            demo_row("baseline", "product-catalog", 1.0, 300.0, 310.0),
            demo_row("muzzle-10", "email", 1.318, 396.4, 401.4),
        ],
    }

    def test_one_entry_per_service_variant_and_measure(self):
        self.assertEqual(entries(self.results), [
            {"name": "email/baseline/spans_per_trace", "unit": "spans", "value": 4.0},
            {"name": "email/baseline/trace_bytes", "unit": "bytes", "value": 712.0},
            {"name": "email/baseline/otlp_bytes_per_request", "unit": "bytes", "value": 717.0},
            {"name": "email/muzzle-10/spans_per_trace", "unit": "spans", "value": 1.318},
            {"name": "email/muzzle-10/trace_bytes", "unit": "bytes", "value": 396.4},
            {"name": "email/muzzle-10/otlp_bytes_per_request", "unit": "bytes", "value": 401.4},
        ])

    def test_a_failed_run_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "checks failed"):
            entries({**self.results, "passed": False})

    def test_a_run_from_before_the_checks_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "checks failed"):
            entries({"rows": self.results["rows"]})


class GitLab(unittest.TestCase):
    types = {
        "Projects::TreeController#show": {"traces": 3, "spans_per_trace": 100.0, "bytes_per_trace": 30000.0},
        "HTTP GET /api/v4/projects/:id": {"traces": 1, "spans_per_trace": 60.0, "bytes_per_trace": 20000.0},
    }
    results = {"passed": True, "variants": [gitlab_variant("muzzle-10", 112.8, 41892.6, types)]}

    def test_request_traces_are_pooled_by_trace_count(self):
        self.assertEqual(entries(self.results), [
            {"name": "gitlab/muzzle-10/spans_per_request", "unit": "spans", "value": 112.8},
            {"name": "gitlab/muzzle-10/otlp_bytes_per_request", "unit": "bytes", "value": 41892.6},
            {"name": "gitlab/muzzle-10/spans_per_trace", "unit": "spans", "value": 90.0},
            {"name": "gitlab/muzzle-10/trace_bytes", "unit": "bytes", "value": 27500.0},
        ])

    def test_a_failed_run_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "checks failed"):
            entries({**self.results, "passed": False})


if __name__ == "__main__":
    unittest.main()
