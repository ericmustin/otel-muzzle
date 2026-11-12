# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from typing import Any

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanKind, TraceState
from opentelemetry.util.types import Attributes

from otel_muzzle import MuzzleSampler
from tests.helpers import (
    LOUD_TRACE_ID,
    QUIET_TRACE_ID,
    VENDOR,
    VERBOSE,
    local_parent,
    ratio_env_vectors,
    ratio_vectors,
    remote_parent,
)

DEFAULT_DELEGATE: Sampler = ParentBased(ALWAYS_ON)


class FixedSampler(Sampler):
    """A delegate that returns a fixed result and records what it was asked."""

    def __init__(self, result: SamplingResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        self.calls.append(
            {
                "parent_context": parent_context,
                "trace_id": trace_id,
                "name": name,
                "kind": kind,
                "attributes": attributes,
                "links": links,
                "trace_state": trace_state,
            }
        )
        return self.result

    def get_description(self) -> str:
        return "Fixed"


def muzzle(ratio: float = 0.0, delegate: Sampler = DEFAULT_DELEGATE) -> MuzzleSampler:
    return MuzzleSampler(delegate, ratio=ratio)


def sample(
    sampler: Sampler, context: Context | None, kind: SpanKind | None = SpanKind.INTERNAL, attributes: Attributes = None
) -> SamplingResult:
    """The sampler's result for one span, called the way the SDK's Tracer.start_span calls it."""
    parent = trace.get_current_span(context).get_span_context()
    trace_id = parent.trace_id if parent.is_valid else QUIET_TRACE_ID
    return sampler.should_sample(context, trace_id, "span", kind, attributes, ())


def recording(result: SamplingResult) -> bool:
    return result.decision is not Decision.DROP


def sampled(result: SamplingResult) -> bool:
    return result.decision is Decision.RECORD_AND_SAMPLE


class TestKeepTable:
    def test_drops_internal_span_under_local_parent(self) -> None:
        result = sample(muzzle(), local_parent())
        assert result.decision is Decision.DROP
        assert not result.attributes

    def test_missing_kind_counts_as_internal(self) -> None:
        assert not recording(sample(muzzle(), local_parent(), kind=None))

    @pytest.mark.parametrize("kind", [SpanKind.SERVER, SpanKind.CLIENT, SpanKind.PRODUCER, SpanKind.CONSUMER])
    def test_keeps_every_other_kind_under_local_parent(self, kind: SpanKind) -> None:
        assert sampled(sample(muzzle(), local_parent(), kind=kind))

    def test_keeps_internal_root(self) -> None:
        assert sampled(sample(muzzle(), Context()))

    def test_keeps_internal_span_under_remote_parent(self) -> None:
        assert sampled(sample(muzzle(), remote_parent()))

    def test_keeps_internal_span_when_tracestate_says_verbose(self) -> None:
        assert sampled(sample(muzzle(), local_parent(trace_state=VERBOSE)))

    @pytest.mark.parametrize(
        "trace_state",
        [
            TraceState([("muzzle", "VERBOSE")]),
            TraceState([("muzzle", "quiet")]),
            TraceState([("vendor", "verbose")]),
            TraceState([("muzzle-x", "verbose")]),
        ],
    )
    def test_other_tracestate_values_do_not_force_verbose(self, trace_state: TraceState) -> None:
        assert not recording(sample(muzzle(), local_parent(trace_state=trace_state)))

    def test_keeps_internal_span_when_ratio_selects_the_trace(self) -> None:
        assert sampled(sample(muzzle(ratio=0.1), local_parent(trace_id=LOUD_TRACE_ID)))
        assert not recording(sample(muzzle(ratio=0.1), local_parent(trace_id=QUIET_TRACE_ID)))

    @pytest.mark.parametrize("case", ratio_vectors(), ids=lambda c: f"{c['trace_id']}@{c['ratio']}")
    def test_ratio_matches_the_shared_vectors(self, case: dict[str, Any]) -> None:
        # An INTERNAL span under a sampled local parent without muzzle=verbose is kept exactly when the ratio selects
        # its trace id.
        result = sample(muzzle(ratio=case["ratio"]), local_parent(trace_id=int(case["trace_id"], 16)))
        assert sampled(result) is case["verbose"]

    @pytest.mark.parametrize(
        "trace_id",
        [
            0x0000000000000000A3CE929DFFFFFFFF,  # a 64-bit B3 or Jaeger id, zero-padded to 128 bits
            0x6720F3A500000000A3CE929DFFFFFFFF,  # Datadog 128-bit: epoch seconds, 4 zero bytes, 64 random bits
            0x6720F3A5FFFFFFFFFFFFFFFFFFFFFFFF,  # X-Ray: epoch seconds, then 96 random bits
        ],
        ids=["padded-64-bit", "datadog-128-bit", "x-ray"],
    )
    def test_zero_or_time_based_leading_bytes_do_not_make_a_trace_verbose(self, trace_id: int) -> None:
        # Only bytes 12-15 count. An earlier rule read bytes 4-7, which are zero in padded and Datadog ids.
        for ratio in (0.1, 0.5, 0.9):
            assert not recording(sample(muzzle(ratio=ratio), local_parent(trace_id=trace_id)))
        assert sampled(sample(muzzle(ratio=0.1), local_parent(trace_id=trace_id & ~0xFFFFFFFF)))

    def test_reads_the_parent_from_the_current_context_when_none_is_given(self) -> None:
        token = context_api.attach(local_parent())
        try:
            assert not recording(muzzle().should_sample(None, QUIET_TRACE_ID, "span", SpanKind.INTERNAL))
        finally:
            context_api.detach(token)


class TestDelegate:
    @pytest.mark.parametrize("context", [Context(), remote_parent(trace_state=VERBOSE)], ids=["root", "remote-parent"])
    def test_delegate_drop_wins(self, context: Context) -> None:
        delegate = FixedSampler(SamplingResult(Decision.DROP, None, VERBOSE))
        result = sample(muzzle(ratio=1.0, delegate=delegate), context, kind=SpanKind.SERVER)
        assert result is delegate.result

    def test_delegate_drop_is_returned_as_is_where_muzzle_would_drop_too(self) -> None:
        # Not a DROP muzzle builds: no tracestate fallback to the parent's.
        delegate = FixedSampler(SamplingResult(Decision.DROP, None, None))
        assert sample(muzzle(delegate=delegate), local_parent(trace_state=VENDOR)) is delegate.result

    @pytest.mark.parametrize("decision", [Decision.RECORD_AND_SAMPLE, Decision.RECORD_ONLY])
    def test_kept_span_returns_the_delegates_result_unchanged(self, decision: Decision) -> None:
        delegate = FixedSampler(SamplingResult(decision, {"a": 1}, VENDOR))
        assert sample(muzzle(delegate=delegate), local_parent(), kind=SpanKind.CLIENT) is delegate.result

    def test_record_only_internal_span_is_dropped_too(self) -> None:
        delegate = FixedSampler(SamplingResult(Decision.RECORD_ONLY, {"a": 1}, VENDOR))
        assert sample(muzzle(delegate=delegate), local_parent()).decision is Decision.DROP

    def test_delegate_sees_the_original_arguments(self) -> None:
        delegate = FixedSampler(SamplingResult(Decision.RECORD_AND_SAMPLE))
        context = local_parent()
        links = (Link(trace.get_current_span(remote_parent()).get_span_context(), {"link": True}),)
        muzzle(delegate=delegate).should_sample(context, QUIET_TRACE_ID, "span", SpanKind.CLIENT, {"k": "v"}, links)
        assert delegate.calls == [
            {
                "parent_context": context,
                "trace_id": QUIET_TRACE_ID,
                "name": "span",
                "kind": SpanKind.CLIENT,
                "attributes": {"k": "v"},
                "links": links,
                "trace_state": None,
            }
        ]

    def test_delegate_sees_an_explicit_trace_state(self) -> None:
        delegate = FixedSampler(SamplingResult(Decision.RECORD_AND_SAMPLE))
        muzzle(delegate=delegate).should_sample(local_parent(), QUIET_TRACE_ID, "span", trace_state=VENDOR)
        assert delegate.calls[0]["trace_state"] is VENDOR

    def test_works_with_a_delegate_that_takes_only_what_the_sdk_passes(self) -> None:
        # The SDK calls should_sample(context, trace_id, name, kind, attributes, links) positionally. A delegate that
        # worked in the provider before otel-muzzle wrapped it must keep working.
        # Built with type(): a subclass written out would not type-check, since it lacks the trace_state parameter.
        def should_sample(
            self: Sampler, ctx: Any, tid: Any, span_name: Any, span_kind: Any, attrs: Any, span_links: Any
        ) -> SamplingResult:
            return SamplingResult(Decision.RECORD_AND_SAMPLE)

        Minimal = type(
            "Minimal", (Sampler,), {"should_sample": should_sample, "get_description": lambda self: "Minimal"}
        )

        assert not recording(sample(muzzle(delegate=Minimal()), local_parent()))
        assert sampled(sample(muzzle(delegate=Minimal()), local_parent(), kind=SpanKind.CLIENT))

    def test_drop_keeps_the_delegates_tracestate(self) -> None:
        delegate = FixedSampler(SamplingResult(Decision.RECORD_AND_SAMPLE, None, VENDOR))
        assert (
            sample(muzzle(delegate=delegate), local_parent(trace_state=TraceState([("other", "y")]))).trace_state
            is VENDOR
        )

    def test_drop_falls_back_to_the_parents_tracestate(self) -> None:
        delegate = FixedSampler(SamplingResult(Decision.RECORD_AND_SAMPLE, None, None))
        # SamplingResult turns a missing trace_state into None, so the parent's is the only one left.
        assert delegate.result.trace_state is None
        assert sample(muzzle(delegate=delegate), local_parent(trace_state=VENDOR)).trace_state == VENDOR

    def test_default_delegate_is_parent_based_always_on(self) -> None:
        sampler = MuzzleSampler(ratio=0.0)
        assert sampler.delegate.get_description() == DEFAULT_DELEGATE.get_description()
        assert not recording(sample(sampler, remote_parent(sampled=False), kind=SpanKind.SERVER))
        assert sampled(sample(sampler, Context(), kind=SpanKind.SERVER))

    def test_delegate_is_positional_or_keyword(self) -> None:
        delegate = FixedSampler(SamplingResult(Decision.DROP))
        assert MuzzleSampler(delegate).delegate is delegate
        assert MuzzleSampler(delegate=delegate).delegate is delegate


class TestRatio:
    def test_ratio_defaults_to_one_in_ten(self) -> None:
        assert MuzzleSampler().ratio == 0.1

    @pytest.mark.parametrize(("raw", "expected"), [(" 0.25 ", 0.25), ("0", 0.0), ("1", 1.0), ("1e-1", 0.1), ("", 0.1)])
    def test_ratio_comes_from_the_environment(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: float) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", raw)
        assert MuzzleSampler().ratio == expected

    @pytest.mark.parametrize("raw", ["abc", "1.5", "-0.1", "NaN", "inf", "0,5"])
    def test_invalid_environment_ratio_falls_back_to_the_default_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", raw)
        with caplog.at_level(logging.WARNING, logger="otel_muzzle"):
            assert MuzzleSampler().ratio == 0.1
        (record,) = caplog.records
        assert record.levelno == logging.WARNING
        assert record.name.startswith("otel_muzzle")
        assert record.getMessage() == (
            f'otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO must be a number within [0, 1], got "{raw}"; using 0.1'
        )

    @pytest.mark.parametrize("case", ratio_env_vectors(), ids=lambda c: json.dumps(c["raw"]))
    def test_environment_ratio_matches_every_case_of_ratio_env_vectors(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, case: dict[str, Any]
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", case["raw"])
        with caplog.at_level(logging.WARNING, logger="otel_muzzle"):
            assert MuzzleSampler().ratio == case["ratio"]
        messages = [record.getMessage() for record in caplog.records]
        if case["warns"]:
            (message,) = messages
            assert message.startswith("otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO must be a number within [0, 1], got ")
            assert message.endswith("; using 0.1")
        else:
            assert messages == []

    def test_an_explicit_ratio_ignores_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "abc")
        with caplog.at_level(logging.WARNING):
            assert MuzzleSampler(ratio=0.5).ratio == 0.5
        assert caplog.records == []

    @pytest.mark.parametrize("ratio", [0, 1, 0.5])
    def test_ratio_is_stored_as_a_float(self, ratio: float) -> None:
        sampler = MuzzleSampler(ratio=ratio)
        assert sampler.ratio == ratio
        assert type(sampler.ratio) is float

    @pytest.mark.parametrize("ratio", [-0.1, 1.1, math.nan, math.inf, -math.inf])
    def test_out_of_range_ratio_argument_raises_value_error(self, ratio: float) -> None:
        with pytest.raises(ValueError, match=r"otel-muzzle: ratio must be a number within \[0, 1\], got"):
            MuzzleSampler(ratio=ratio)

    @pytest.mark.parametrize("ratio", ["0.5", True, False, b"1", [0.5]])
    def test_non_number_ratio_argument_raises_type_error(self, ratio: Any) -> None:
        with pytest.raises(TypeError, match=r"otel-muzzle: ratio must be a number within \[0, 1\], got"):
            MuzzleSampler(ratio=ratio)

    def test_description_names_ratio_and_delegate(self) -> None:
        fixed = FixedSampler(SamplingResult(Decision.DROP))
        assert MuzzleSampler(fixed, ratio=0.1).get_description() == "MuzzleSampler{ratio=0.1, delegate=Fixed}"
        assert MuzzleSampler(fixed, ratio=1).get_description() == "MuzzleSampler{ratio=1.0, delegate=Fixed}"
        assert MuzzleSampler(ratio=0.0).get_description() == (
            f"MuzzleSampler{{ratio=0.0, delegate={DEFAULT_DELEGATE.get_description()}}}"
        )
