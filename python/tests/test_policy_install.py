# SPDX-License-Identifier: Apache-2.0
"""Policy mode in the sampler, in install(), and through the start_span patch."""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import Link, NonRecordingSpan, SpanContext, SpanKind, TraceFlags, TraceState
from opentelemetry.util.types import Attributes

from otel_muzzle import MuzzleSampler, _patch, install
from otel_muzzle.policy import _runtime
from otel_muzzle.policy.parser import parse_policy_set
from otel_muzzle.policy.span_sampling import SpanSamplingImplementer
from tests.helpers import (
    EXAMPLE_POLICY,
    POLICY_DROP_TRACE_ID,
    POLICY_KEEP_TRACE_ID,
    VERBOSE,
    by_name,
    local_parent,
    parent_id,
    policy_vectors,
    remote_parent,
    sdk_tracer,
    span_id,
)

KEEP_ALL = '{"policies": [{"id": "all", "spanSampling": {"sampling": {"percentage": 100}}}]}'
DROP_ALL = '{"policies": [{"id": "none", "spanSampling": {"sampling": {"percentage": 0}}}]}'


class Recording(Sampler):
    """A delegate that records every span and returns a fixed tracestate of its own, or none."""

    def __init__(self, trace_state: TraceState | None = None, decision: Decision = Decision.RECORD_AND_SAMPLE) -> None:
        self.result = SamplingResult(decision, {"from": "delegate"}, trace_state)
        self.calls: list[tuple[Any, ...]] = []

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
        self.calls.append((parent_context, trace_id, name, kind, attributes, links))
        return self.result

    def get_description(self) -> str:
        return "Recording"


def tracestate_of(result: SamplingResult) -> dict[str, str]:
    return dict(result.trace_state.items()) if result.trace_state is not None else {}


def sampler_of(tracer: Tracer) -> Sampler:
    """The tracer's sampler, typed as the base class so that mypy does not keep an earlier narrowing."""
    return tracer.sampler


def policies_of(text: str) -> SpanSamplingImplementer:
    policies = SpanSamplingImplementer()
    policies.apply_policies(parse_policy_set(text))
    return policies


def example() -> SpanSamplingImplementer:
    return policies_of(EXAMPLE_POLICY.read_text())


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    shutil.copy(EXAMPLE_POLICY, path)
    return path


def server_work_client(tracer: trace.Tracer, context: Context) -> None:
    with (
        tracer.start_as_current_span("GET /", context=context, kind=SpanKind.SERVER),
        tracer.start_as_current_span("work"),
        tracer.start_as_current_span("inner"),
        tracer.start_as_current_span("GET upstream", kind=SpanKind.CLIENT),
    ):
        pass


def vector_context(span: dict[str, Any]) -> Context:
    if span["parent"] == "none":
        return Context()
    parent = SpanContext(
        int(span["trace_id"], 16),
        int(span["parent_span_id"], 16),
        span["parent"] == "remote",
        TraceFlags(TraceFlags.SAMPLED),
        TraceState([(key, value) for key, value in span["tracestate"]]),
    )
    return trace.set_span_in_context(NonRecordingSpan(parent), Context())


class TestPolicyMode:
    def test_decisions_match_the_shared_vectors(self) -> None:
        vectors = policy_vectors("decision")
        samplers: dict[str, SpanSamplingImplementer] = {}
        for name, policy_set in vectors["policy_sets"].items():
            samplers[name] = SpanSamplingImplementer()
            if not policy_set["malformed"]:
                samplers[name].apply_policies(parse_policy_set(policy_set["text"]))
        assert len(vectors["cases"]) > 190
        for case in vectors["cases"]:
            span = case["span"]
            own = span["result_tracestate"]
            delegate = Recording(None if own is None else TraceState([(key, value) for key, value in own]))
            sampler = MuzzleSampler(delegate, policies=samplers[case["set"]])
            result = sampler.should_sample(
                vector_context(span),
                int(span["trace_id"], 16),
                span["name"],
                None if span["kind"] is None else SpanKind[span["kind"]],
                span["attributes"],
            )
            label = (case["set"], case["name"])
            if case["keep"]:
                assert result is delegate.result, label
            else:
                assert result.decision is Decision.DROP, label
                assert tracestate_of(result) == case["drop_tracestate"], label
                assert not result.attributes, label

    def test_the_delegates_drop_wins(self) -> None:
        for delegate in (ALWAYS_OFF, Recording(decision=Decision.DROP)):
            sampler = MuzzleSampler(delegate, policies=policies_of(KEEP_ALL))
            result = sampler.should_sample(
                remote_parent(POLICY_KEEP_TRACE_ID), POLICY_KEEP_TRACE_ID, "GET /", SpanKind.SERVER
            )
            assert result.decision is Decision.DROP

    def test_a_kept_span_returns_the_delegates_result_unchanged(self) -> None:
        delegate = Recording(TraceState([("ot", "th:0"), ("vendor", "x")]))
        sampler = MuzzleSampler(delegate, policies=example())
        context = local_parent(POLICY_KEEP_TRACE_ID)
        result = sampler.should_sample(context, POLICY_KEEP_TRACE_ID, "work", SpanKind.INTERNAL, {"a": 1}, [])
        assert result is delegate.result
        assert delegate.calls == [(context, POLICY_KEEP_TRACE_ID, "work", SpanKind.INTERNAL, {"a": 1}, [])]

    def test_a_record_only_span_can_be_dropped(self) -> None:
        sampler = MuzzleSampler(Recording(decision=Decision.RECORD_ONLY), policies=example())
        result = sampler.should_sample(local_parent(POLICY_DROP_TRACE_ID), POLICY_DROP_TRACE_ID, "work")
        assert result.decision is Decision.DROP

    def test_a_policy_drop_erases_th(self) -> None:
        state = TraceState([("ot", "th:0;rv:00000000000000"), ("vendor", "x")])
        sampler = MuzzleSampler(policies=example())
        result = sampler.should_sample(
            local_parent(POLICY_KEEP_TRACE_ID, trace_state=state), POLICY_KEEP_TRACE_ID, "work"
        )
        assert result.decision is Decision.DROP
        assert tracestate_of(result) == {"ot": "rv:00000000000000", "vendor": "x"}

    def test_a_policy_drop_without_th_returns_the_tracestate_itself(self) -> None:
        state = TraceState([("vendor", "x")])
        sampler = MuzzleSampler(policies=example())
        result = sampler.should_sample(
            local_parent(POLICY_DROP_TRACE_ID, trace_state=state), POLICY_DROP_TRACE_ID, "work"
        )
        assert result.decision is Decision.DROP and result.trace_state is state

    def test_the_built_in_rule_is_not_applied(self) -> None:
        sampler = MuzzleSampler(policies=policies_of(KEEP_ALL))
        result = sampler.should_sample(
            local_parent(POLICY_DROP_TRACE_ID), POLICY_DROP_TRACE_ID, "work", SpanKind.INTERNAL
        )
        assert result.decision is Decision.RECORD_AND_SAMPLE
        dropping = MuzzleSampler(policies=policies_of(DROP_ALL))
        server = dropping.should_sample(
            remote_parent(trace_state=VERBOSE), POLICY_KEEP_TRACE_ID, "GET /", SpanKind.SERVER
        )
        assert server.decision is Decision.DROP

    def test_description_and_properties(self) -> None:
        policies = example()
        sampler = MuzzleSampler(ALWAYS_ON, policies=policies)
        assert sampler.get_description() == "MuzzleSampler{mode=policy, delegate=AlwaysOnSampler}"
        assert sampler.policies is policies and sampler.ratio is None and sampler.delegate is ALWAYS_ON
        assert MuzzleSampler(ALWAYS_ON).policies is None

    def test_the_policy_file_argument(self, policy_file: Path) -> None:
        sampler = MuzzleSampler(policy_file=str(policy_file))
        assert sampler.policies is _runtime.from_file(str(policy_file))
        assert MuzzleSampler(policy_file=policy_file).policies is sampler.policies

    def test_the_policy_file_from_the_environment(self, policy_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_POLICY_FILE", str(policy_file))
        sampler = MuzzleSampler()
        assert sampler.policies is _runtime.from_file(str(policy_file)) and sampler.ratio is None

    def test_an_explicit_ratio_is_validated_then_ignored_with_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with pytest.raises(ValueError, match="ratio must be"):
            MuzzleSampler(ratio=2.0, policies=example())
        with caplog.at_level(logging.WARNING):
            sampler = MuzzleSampler(ratio=0.5, policies=example())
        assert sampler.ratio is None
        (record,) = caplog.records
        assert record.getMessage() == "otel-muzzle: ratio 0.5 is ignored in policy mode"

    def test_an_unreadable_policy_file_drops_nothing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            sampler = MuzzleSampler(policy_file=str(tmp_path / "missing.json"))
        result = sampler.should_sample(
            local_parent(POLICY_DROP_TRACE_ID), POLICY_DROP_TRACE_ID, "work", SpanKind.INTERNAL
        )
        assert result.decision is Decision.RECORD_AND_SAMPLE
        assert sampler.policies is not None and not sampler.policies.active
        assert len(caplog.records) == 1 and "no policy is in effect" in caplog.records[0].getMessage()

    def test_without_a_policy_file_nothing_changes(self) -> None:
        sampler = MuzzleSampler()
        assert sampler.policies is None and sampler.ratio == 0.1
        assert sampler.get_description().startswith("MuzzleSampler{ratio=0.1, delegate=ParentBased{")


class TestInstallWithAPolicyFile:
    def test_children_of_a_policy_dropped_span_attach_to_its_parent(
        self, policy_file: Path, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install(policy_file=str(policy_file))
        server_work_client(sdk_tracer(provider(), "app"), remote_parent(POLICY_DROP_TRACE_ID))
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["GET /"])

    def test_a_trace_the_percentage_selects_keeps_every_span(
        self, policy_file: Path, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install(policy_file=str(policy_file))
        server_work_client(sdk_tracer(provider(), "app"), remote_parent(POLICY_KEEP_TRACE_ID))
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream", "inner", "work"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["inner"])

    def test_muzzle_verbose_keeps_every_span(
        self, policy_file: Path, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install(policy_file=str(policy_file))
        server_work_client(sdk_tracer(provider(), "app"), remote_parent(POLICY_DROP_TRACE_ID, trace_state=VERBOSE))
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream", "inner", "work"]

    def test_an_explicit_rv_decides_instead_of_the_trace_id(
        self, policy_file: Path, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install(policy_file=str(policy_file))
        state = TraceState([("ot", "rv:00000000000000")])
        server_work_client(sdk_tracer(provider(), "app"), remote_parent(POLICY_KEEP_TRACE_ID, trace_state=state))
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream"]
        assert spans["GET upstream"].context.trace_state.get("ot") == "rv:00000000000000"

    def test_install_reads_the_policy_file_from_the_environment(
        self, policy_file: Path, monkeypatch: pytest.MonkeyPatch, provider: Callable[..., TracerProvider]
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_POLICY_FILE", str(policy_file))
        install()
        tracer = sdk_tracer(provider(sampler=ALWAYS_ON), "app")
        tracer.start_span("first").end()
        assert tracer.sampler.get_description() == "MuzzleSampler{mode=policy, delegate=AlwaysOnSampler}"
        assert _patch.install(None) is None

    def test_the_environment_ratio_is_ignored_with_a_warning(
        self, policy_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "1")
        with caplog.at_level(logging.WARNING):
            install(policy_file=str(policy_file))
        assert [record.getMessage() for record in caplog.records] == [
            f"otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO is ignored, because a policy file is configured: {policy_file}"
        ]

    def test_an_explicit_ratio_is_ignored_with_a_warning(
        self, policy_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            install(ratio=0.5, policy_file=str(policy_file))
        assert [record.getMessage() for record in caplog.records] == [
            "otel-muzzle: ratio 0.5 is ignored in policy mode"
        ]

    def test_install_again_keeps_its_policy_file_and_warns(
        self, policy_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        other = tmp_path / "other.json"
        other.write_text(KEEP_ALL)
        install(policy_file=str(policy_file))
        with caplog.at_level(logging.WARNING):
            install(policy_file=str(policy_file))
            install()
            assert caplog.records == []
            install(policy_file=str(other))
        assert [record.getMessage() for record in caplog.records] == [
            f"otel-muzzle: already installed with the policy file {policy_file}, ignoring the policy file {other}"
        ]

    def test_a_policy_file_after_a_plain_install_is_ignored_with_a_warning(
        self, policy_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        install()
        with caplog.at_level(logging.WARNING):
            install(policy_file=str(policy_file))
        assert [record.getMessage() for record in caplog.records] == [
            f"otel-muzzle: already installed without a policy file, ignoring the policy file {policy_file}"
        ]
        assert _patch.install(None) == 0.1

    def test_install_with_the_sdk_disabled_reads_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        with caplog.at_level(logging.WARNING):
            install(policy_file=str(tmp_path / "missing.json"))
        assert caplog.records == [] and not _patch.is_patched()

    def test_a_policy_file_that_is_not_a_path_raises(self) -> None:
        with pytest.raises(TypeError, match="policy_file must be a path"):
            install(policy_file=5)  # type: ignore[arg-type]
        assert not _patch.is_patched()

    def test_reload_swaps_the_set_for_existing_tracers(
        self, policy_file: Path, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install(policy_file=str(policy_file))
        tracer = sdk_tracer(provider(), "app")
        server_work_client(tracer, remote_parent(POLICY_DROP_TRACE_ID))
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream"]
        exporter.clear()
        policy_file.write_text(KEEP_ALL)
        sampler = tracer.sampler
        assert isinstance(sampler, MuzzleSampler) and sampler.policies is not None
        assert _runtime.provider_for(sampler.policies).reload()
        server_work_client(tracer, remote_parent(POLICY_DROP_TRACE_ID))
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream", "inner", "work"]

    def test_uninstall_gives_each_tracer_its_own_sampler_back(
        self, policy_file: Path, provider: Callable[..., TracerProvider]
    ) -> None:
        own = ParentBased(ALWAYS_ON)
        install(policy_file=str(policy_file))
        tracer = sdk_tracer(provider(sampler=own), "app")
        tracer.start_span("first").end()
        assert isinstance(tracer.sampler, MuzzleSampler)
        _patch.uninstall()
        tracer.start_span("second").end()
        assert sampler_of(tracer) is own

    def test_install_after_uninstall_can_change_mode(
        self, policy_file: Path, provider: Callable[..., TracerProvider]
    ) -> None:
        install(ratio=0.3)
        tracer = sdk_tracer(provider(), "app")
        tracer.start_span("first").end()
        assert isinstance(tracer.sampler, MuzzleSampler) and tracer.sampler.ratio == 0.3
        _patch.uninstall()
        install(policy_file=str(policy_file))
        tracer.start_span("second").end()
        assert isinstance(tracer.sampler, MuzzleSampler)
        assert tracer.sampler.ratio is None and tracer.sampler.policies is _runtime.from_file(str(policy_file))

    def test_the_example_policy_is_valid_json_with_one_policy(self) -> None:
        document = json.loads(EXAMPLE_POLICY.read_text())
        assert [policy["id"] for policy in document["policies"]] == ["drop-internal-spans"]
