# SPDX-License-Identifier: Apache-2.0
"""Policy mode against the SDK's own consistent probability sampling code, where the SDK has it.

``opentelemetry.sdk.trace._sampling_experimental`` is the SDK's implementation of the OpenTelemetry tracestate
probability sampling specification (SDK 1.37 and later). otel-muzzle does not import it at runtime; these tests check
that both read the same randomness and thresholds and compute the same full-precision threshold.
"""

from __future__ import annotations

import pytest
from opentelemetry.trace import TraceState

from otel_muzzle.policy._threshold import parse_ot, randomness, threshold
from tests.helpers import policy_vectors

_util = pytest.importorskip("opentelemetry.sdk.trace._sampling_experimental._util")
_trace_state = pytest.importorskip("opentelemetry.sdk.trace._sampling_experimental._trace_state")


class TestOracle:
    def test_full_precision_thresholds_agree_with_the_sdk(self) -> None:
        checked = 0
        for case in policy_vectors("threshold")["cases"]:
            if case["precision"] != 14:
                continue
            probability = case["percentage"] / 100
            scaled = probability * 2**56
            if scaled - int(scaled) == 0.5:
                continue  # the SDK rounds a tie to even, the specification's reference rounds it up
            assert threshold(probability, 14) == _util.calculate_threshold(probability), case
            checked += 1
        assert checked > 30

    def test_valid_th_and_rv_read_the_same_as_the_sdk(self) -> None:
        checked = 0
        for case in policy_vectors("tracestate")["cases"]:
            if case["ot"] is None or case["ot"] == "":
                continue
            rv, th = parse_ot(case["ot"])
            theirs = _trace_state.OtelTraceState.parse(TraceState([("ot", case["ot"])]))
            if th is not None:
                assert theirs.threshold == th, case
                checked += 1
            if rv is not None:
                assert theirs.random_value == rv, case
                checked += 1
        assert checked > 12

    def test_randomness_is_the_sdks_low_56_bits(self) -> None:
        for case in policy_vectors("tracestate")["cases"]:
            trace_id = int(case["trace_id"], 16)
            assert randomness(trace_id, None) == trace_id & _util.MAX_RANDOM_VALUE
