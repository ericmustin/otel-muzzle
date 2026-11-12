# SPDX-License-Identifier: Apache-2.0
"""The start_span patch on its own: tracers get their sampler from the test, and nothing wraps it lazily."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from contextvars import Token

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, Context
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import (
    INVALID_SPAN_ID,
    INVALID_TRACE_ID,
    Link,
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    TraceFlags,
    TraceState,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.util.types import Attributes

from otel_muzzle import MuzzleSampler, _patch
from tests.helpers import (
    LOUD_TRACE_ID,
    QUIET_TRACE_ID,
    REMOTE_SPAN_ID,
    VENDOR,
    VERBOSE,
    by_name,
    parent_id,
    remote_parent,
    sdk_tracer,
    span_id,
)

SDK_START_SPAN = Tracer.start_span


class DropByName(Sampler):
    """Drops every span whose name starts with "drop", whatever its kind or parent."""

    def __init__(self, delegate: Sampler) -> None:
        self.delegate = delegate

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
        if name.startswith("drop"):
            return SamplingResult(Decision.DROP)
        return self.delegate.should_sample(parent_context, trace_id, name, kind, attributes, links)

    def get_description(self) -> str:
        return "DropByName"


def muzzle(delegate: Sampler | None = None) -> MuzzleSampler:
    return MuzzleSampler(delegate or ParentBased(ALWAYS_ON), ratio=0.0)


@pytest.fixture
def patched(provider: Callable[..., TracerProvider]) -> Callable[..., trace.Tracer]:
    """A tracer sampling with the given sampler (a MuzzleSampler by default), with the patch applied."""

    def build(sampler: Sampler | None = None) -> trace.Tracer:
        _patch.patch()
        return provider(sampler=sampler or muzzle()).get_tracer("test")

    return build


def server_work_client(tracer: trace.Tracer, context: Context | None = None) -> None:
    with (
        tracer.start_as_current_span("GET /", context=context, kind=SpanKind.SERVER),
        tracer.start_as_current_span("work"),
        tracer.start_as_current_span("GET upstream", kind=SpanKind.CLIENT),
    ):
        pass


class RecordOnlyServers(Sampler):
    """Records SERVER spans without sampling them, and samples everything else."""

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
        return SamplingResult(Decision.RECORD_ONLY if kind is SpanKind.SERVER else Decision.RECORD_AND_SAMPLE)

    def get_description(self) -> str:
        return "RecordOnlyServers"


def suppressed() -> Token[Context]:
    return context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))


class TestReparenting:
    def test_children_of_a_dropped_span_attach_to_its_parent(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        server_work_client(patched())
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["GET /"])

    def test_nested_drops_chain_up_to_the_kept_ancestor(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched()
        with (
            tracer.start_as_current_span("GET /", kind=SpanKind.SERVER),
            tracer.start_as_current_span("outer"),
            tracer.start_as_current_span("inner"),
            tracer.start_as_current_span("query", kind=SpanKind.CLIENT),
        ):
            pass
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "query"]
        assert parent_id(spans["query"]) == span_id(spans["GET /"])

    def test_a_dropped_span_carries_its_parents_context(self, patched: Callable[..., trace.Tracer]) -> None:
        tracer = patched()
        with (
            tracer.start_as_current_span(
                "GET /", context=remote_parent(trace_state=VENDOR), kind=SpanKind.SERVER
            ) as server,
            tracer.start_as_current_span("work") as work,
        ):
            assert not work.is_recording()
            assert trace.get_current_span() is work
            server_context, work_context = server.get_span_context(), work.get_span_context()
            assert work_context.trace_id == server_context.trace_id == QUIET_TRACE_ID
            assert work_context.span_id == server_context.span_id
            assert work_context.trace_flags == server_context.trace_flags
            assert work_context.trace_flags.sampled
            assert work_context.trace_state == VENDOR
            assert not work_context.is_remote

    def test_start_span_with_an_explicit_context(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched()
        server = tracer.start_span("GET /", kind=SpanKind.SERVER)
        work = tracer.start_span("work", trace.set_span_in_context(server))
        client = tracer.start_span("GET upstream", trace.set_span_in_context(work), SpanKind.CLIENT)
        for span in (client, work, server):
            span.end()
        assert work.get_span_context() == server.get_span_context()
        assert parent_id(by_name(exporter)["GET upstream"]) == span_id(server)

    def test_finishing_a_dropped_span_leaves_its_parent_running(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched()
        with tracer.start_as_current_span("GET /", kind=SpanKind.SERVER) as server:
            work = tracer.start_span("work")
            work.set_attribute("ignored", True)
            work.add_event("ignored")
            work.set_status(Status(StatusCode.ERROR))
            work.record_exception(RuntimeError("ignored"))
            work.end()
            assert server.is_recording()
        spans = by_name(exporter)
        assert list(spans) == ["GET /"]
        assert "ignored" not in (spans["GET /"].attributes or {})
        assert spans["GET /"].events == ()
        assert spans["GET /"].status.status_code is StatusCode.UNSET

    def test_an_exception_in_a_dropped_span_is_recorded_on_the_kept_ancestor(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched()
        with (
            pytest.raises(RuntimeError, match="boom"),
            tracer.start_as_current_span("GET /", kind=SpanKind.SERVER),
            tracer.start_as_current_span("work"),
        ):
            raise RuntimeError("boom")
        server = by_name(exporter)["GET /"]
        assert [event.name for event in server.events] == ["exception"]
        assert server.status.status_code is StatusCode.ERROR

    def test_a_request_made_under_a_dropped_span_carries_the_kept_ancestors_ids(
        self, patched: Callable[..., trace.Tracer]
    ) -> None:
        tracer = patched()
        carrier: dict[str, str] = {}
        with (
            tracer.start_as_current_span(
                "GET /", context=remote_parent(trace_state=VENDOR), kind=SpanKind.SERVER
            ) as server,
            tracer.start_as_current_span("work"),
        ):
            TraceContextTextMapPropagator().inject(carrier)
        assert carrier == {
            "traceparent": f"00-{QUIET_TRACE_ID:032x}-{span_id(server):016x}-01",
            "tracestate": "vendor=x",
        }


class TestLeftToTheSdk:
    def test_spans_under_an_unsampled_parent_are_left_to_the_sdk(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched()
        with tracer.start_as_current_span(
            "GET /", context=remote_parent(sampled=False), kind=SpanKind.SERVER
        ) as server:
            assert not server.is_recording()
            assert span_id(server) != REMOTE_SPAN_ID
            with tracer.start_as_current_span("work") as work:
                assert span_id(work) != span_id(server)
                assert not work.get_span_context().trace_flags.sampled
        assert exporter.get_finished_spans() == ()

    def test_spans_under_a_recording_but_unsampled_parent_are_left_to_the_sdk(
        self, patched: Callable[..., trace.Tracer]
    ) -> None:
        # The patch checks the parent's sampled flag, not whether the parent is recording.
        tracer = patched(muzzle(RecordOnlyServers()))
        with tracer.start_as_current_span("GET /", kind=SpanKind.SERVER) as server:
            assert server.is_recording()
            assert not server.get_span_context().trace_flags.sampled
            with tracer.start_as_current_span("work") as work:
                assert not work.is_recording()
                assert span_id(work) != span_id(server)
                assert not work.get_span_context().trace_flags.sampled

    def test_spans_dropped_under_a_remote_parent_are_left_to_the_sdk(
        self, patched: Callable[..., trace.Tracer]
    ) -> None:
        tracer = patched(muzzle(DropByName(ALWAYS_ON)))
        span = tracer.start_span("drop me", remote_parent(), SpanKind.SERVER)
        assert span_id(span) != REMOTE_SPAN_ID
        assert not span.get_span_context().trace_flags.sampled

    def test_dropped_roots_are_left_to_the_sdk(self, patched: Callable[..., trace.Tracer]) -> None:
        tracer = patched(muzzle(DropByName(ALWAYS_ON)))
        span = tracer.start_span("drop me", kind=SpanKind.SERVER)
        assert span.get_span_context().is_valid
        assert not span.get_span_context().trace_flags.sampled

    def test_an_explicit_root_is_kept_even_under_a_current_span(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched()
        with tracer.start_as_current_span("GET /", kind=SpanKind.SERVER) as server:
            root = tracer.start_span("job", Context())
            assert root.is_recording()
            root.end()
        job = by_name(exporter)["job"]
        assert job.parent is None
        assert job.context is not None
        assert job.context.trace_id != server.get_span_context().trace_id

    def test_a_dropped_explicit_root_is_left_to_the_sdk_not_given_the_current_span(
        self, patched: Callable[..., trace.Tracer]
    ) -> None:
        # The patch must look at the context the SDK used, not at the current one.
        tracer = patched(muzzle(DropByName(ALWAYS_ON)))
        with tracer.start_as_current_span("GET /", kind=SpanKind.SERVER) as server:
            root = tracer.start_span("drop me", Context())
        assert span_id(root) not in (span_id(server), INVALID_SPAN_ID)
        assert root.get_span_context().trace_id != server.get_span_context().trace_id
        assert not root.get_span_context().trace_flags.sampled

    def test_a_dropped_span_under_an_invalid_parent_flagged_sampled_is_left_to_the_sdk(
        self, patched: Callable[..., trace.Tracer]
    ) -> None:
        # An invalid parent makes the span a root, however its flags read. Handing out that invalid context would make
        # every child start a trace of its own.
        tracer = patched(muzzle(DropByName(ALWAYS_ON)))
        invalid = SpanContext(INVALID_TRACE_ID, INVALID_SPAN_ID, False, TraceFlags(TraceFlags.SAMPLED))
        span = tracer.start_span("drop me", trace.set_span_in_context(NonRecordingSpan(invalid)))
        assert span.get_span_context().is_valid
        assert not span.get_span_context().trace_flags.sampled


class TestSuppression:
    def test_the_python_sdk_does_not_suppress_spans(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        # The upstream fact the patch relies on. Instrumentations check suppress_instrumentation and start no span; the
        # SDK's Tracer.start_span ignores it. If an SDK release starts honouring it, this fails, and the patch needs the
        # suppression check that the Ruby and Node.js patches have.
        tracer = provider(sampler=ParentBased(ALWAYS_ON)).get_tracer("test")
        token = suppressed()
        try:
            server_work_client(tracer)
        finally:
            context_api.detach(token)
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream", "work"]

    def test_suppression_changes_nothing(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        # The SDK has no suppression decision for the patch to preserve, so muzzle behaves exactly as it does
        # outside suppress_instrumentation.
        tracer = patched()
        token = suppressed()
        try:
            server_work_client(tracer)
        finally:
            context_api.detach(token)
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["GET /"])


class TestOtherSamplers:
    def test_a_span_another_sampler_drops_under_a_sampled_parent_becomes_transparent(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched(muzzle(DropByName(ParentBased(ALWAYS_ON))))
        with (
            tracer.start_as_current_span("GET /", kind=SpanKind.SERVER),
            tracer.start_as_current_span("drop me", kind=SpanKind.CLIENT),
            tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT),
        ):
            pass
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "SELECT"]
        assert parent_id(spans["SELECT"]) == span_id(spans["GET /"])

    @pytest.mark.parametrize(
        ("context", "ratio"),
        [(remote_parent(trace_state=VERBOSE), 0.0), (remote_parent(trace_id=LOUD_TRACE_ID), 0.1)],
        ids=["tracestate", "ratio"],
    )
    def test_verbose_traces_keep_every_span(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter, context: Context, ratio: float
    ) -> None:
        _patch.patch()
        tracer = provider(sampler=MuzzleSampler(ratio=ratio)).get_tracer("test")
        server_work_client(tracer, context)
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream", "work"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["work"])

    def test_without_a_muzzle_sampler_the_patch_changes_nothing(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        server_work_client(patched(ParentBased(ALWAYS_ON)))
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream", "work"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["work"])

    def test_without_the_patch_the_subtree_of_a_dropped_span_is_lost(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        # Why the patch exists: the SDK gives "work" a fresh unsampled span id, and ParentBased drops what is under it.
        server_work_client(provider(sampler=muzzle()).get_tracer("test"))
        assert sorted(by_name(exporter)) == ["GET /"]


class TestPatch:
    def test_the_decorator_form_goes_through_the_patch(
        self, patched: Callable[..., trace.Tracer], exporter: InMemorySpanExporter
    ) -> None:
        tracer = patched()

        @tracer.start_as_current_span("work")
        def work() -> None:
            with tracer.start_as_current_span("GET upstream", kind=SpanKind.CLIENT):
                pass

        with tracer.start_as_current_span("GET /", kind=SpanKind.SERVER):
            work()
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["GET /"])

    def test_a_proxy_tracer_goes_through_the_patch(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        tracer = trace.get_tracer("early")  # a ProxyTracer: no provider is set yet
        _patch.patch()
        trace.set_tracer_provider(provider(sampler=muzzle()))
        server_work_client(tracer)
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["GET /"])

    def test_the_patch_reaches_tracers_created_before_it(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        tracer = provider(sampler=muzzle()).get_tracer("test")
        _patch.patch()
        server_work_client(tracer)
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream"]

    def test_patching_twice_wraps_once(self) -> None:
        _patch.patch()
        once = Tracer.start_span
        _patch.patch()
        assert Tracer.start_span is once
        assert getattr(once, "__wrapped__") is SDK_START_SPAN

    def test_the_patch_keeps_the_sdks_signature_and_name(self) -> None:
        _patch.patch()
        assert _patch.is_patched()
        assert inspect.signature(Tracer.start_span) == inspect.signature(SDK_START_SPAN)
        assert Tracer.start_span.__qualname__ == SDK_START_SPAN.__qualname__


class TestInstall:
    def test_install_returns_the_ratio_in_effect(self) -> None:
        assert _patch.install(0.25) == 0.25
        assert _patch.install(0.5) == 0.25

    def test_install_without_a_ratio_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "0.3")
        assert _patch.install(None) == 0.3

    def test_sync_sampler_wraps_rewraps_and_restores(self, provider: Callable[..., TracerProvider]) -> None:
        tracer = sdk_tracer(provider(sampler=ALWAYS_ON))
        _patch._sync_sampler(tracer, 0.2)
        assert isinstance(tracer.sampler, MuzzleSampler) and tracer.sampler.ratio == 0.2
        _patch._sync_sampler(tracer, 0.4)
        assert isinstance(tracer.sampler, MuzzleSampler)
        assert tracer.sampler.ratio == 0.4 and tracer.sampler.delegate is ALWAYS_ON
        _patch._sync_sampler(tracer, None)
        assert sampler_of(tracer) is ALWAYS_ON
        own = MuzzleSampler(ratio=0.9)
        tracer.sampler = own
        _patch._sync_sampler(tracer, 0.2)
        assert tracer.sampler is own


def sampler_of(tracer: Tracer) -> Sampler:
    """The tracer's sampler, typed as the base class so that mypy does not keep an earlier narrowing."""
    return tracer.sampler
