# SPDX-License-Identifier: Apache-2.0
"""Policy mode: thresholds, the `ot` tracestate member, and randomness."""

from __future__ import annotations

import pytest

from otel_muzzle.policy import _threshold
from otel_muzzle.policy._threshold import MAX_THRESHOLD, parse_ot, proportional, randomness, threshold
from tests.helpers import policy_vectors


def _hex(value: int) -> str:
    return f"{value:014x}"


class TestThreshold:
    def test_threshold_matches_the_shared_vectors(self) -> None:
        cases = policy_vectors("threshold")["cases"]
        assert len(cases) > 300
        for case in cases:
            got = threshold(case["percentage"] / 100, case["precision"])
            assert _hex(got) == case["threshold"], case

    def test_proportional_matches_the_shared_vectors(self) -> None:
        cases = policy_vectors("threshold")["proportional"]
        assert len(cases) > 200
        for case in cases:
            got = proportional(int(case["arriving"], 16), case["percentage"] / 100, case["precision"])
            assert _hex(got) == case["threshold"], case

    def test_zero_keeps_nothing_and_one_keeps_everything(self) -> None:
        assert threshold(0.0, 4) == MAX_THRESHOLD == 2**56
        assert threshold(1.0, 4) == 0

    def test_ten_percent_at_the_default_precision(self) -> None:
        assert threshold(0.1, 4) == 0xE6660000000000


class TestTracestate:
    def test_ot_parsing_matches_the_shared_vectors(self) -> None:
        cases = policy_vectors("tracestate")["cases"]
        assert len(cases) > 40
        for case in cases:
            rv, th = parse_ot(case["ot"])
            assert (None if rv is None else _hex(rv)) == case["rv"], case
            assert (None if th is None else _hex(th)) == case["th"], case
            assert _hex(randomness(int(case["trace_id"], 16), rv)) == case["randomness"], case

    def test_rv_wins_over_the_trace_id(self) -> None:
        assert randomness(0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF, 0) == 0
        assert randomness(0x4BF92F3577B34DA6A3CE929D0E0E4736, None) == 0xCE929D0E0E4736

    @pytest.mark.parametrize(
        ("ot", "expected"),
        [
            ("th:8", None),
            ("th:8;rv:00000000000000", "rv:00000000000000"),
            ("ext:1;th:8;x:2", "ext:1;x:2"),
            ("rv:1", "rv:1"),
        ],
    )
    def test_erase_th_removes_only_th(self, ot: str, expected: str | None) -> None:
        assert _threshold.erase_th(ot) == expected
