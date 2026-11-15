# SPDX-License-Identifier: Apache-2.0
"""Tests for analyze.py's checks. Run with `python3 -m unittest -q` from bench/."""

import unittest

from analyze import failures


def row(**changes):
    """One results.json row that passes every check."""
    base = {"label": "otel-muzzle, ratio 10% (default)", "service": "email", "requests": 1000, "ok_rate": 1,
            "server_spans": 1000, "dangling_parent_refs": 0, "rule_mismatches": 0}
    return {**base, **changes}


class Failures(unittest.TestCase):
    def test_a_clean_run_passes(self):
        self.assertEqual(failures([row(), row(service="product-catalog", ok_rate="")]), [])

    def test_lost_server_spans_fail(self):
        self.assertEqual(failures([row(server_spans=998)]),
                         ["otel-muzzle, ratio 10% (default), email: 998 server spans for 1,000 requests"])

    def test_a_service_without_requests_fails(self):
        self.assertEqual(failures([row(requests=0, server_spans=0)]),
                         ["otel-muzzle, ratio 10% (default), email: no requests"])

    def test_dangling_parents_fail(self):
        self.assertEqual(failures([row(dangling_parent_refs=3)]),
                         ["otel-muzzle, ratio 10% (default), email: 3 dangling parents"])

    def test_rule_mismatches_fail(self):
        self.assertEqual(failures([row(rule_mismatches=2)]),
                         ["otel-muzzle, ratio 10% (default), email: 2 rule mismatches"])

    def test_failed_requests_fail(self):
        self.assertEqual(failures([row(ok_rate=0.995)]),
                         ["otel-muzzle, ratio 10% (default), email: 99.5% of requests succeeded"])


if __name__ == "__main__":
    unittest.main()
