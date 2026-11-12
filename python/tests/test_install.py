# SPDX-License-Identifier: Apache-2.0
"""otel_muzzle.install(), in process. The conftest fixture undoes the class-level patch after each test."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased
from opentelemetry.trace import NoOpTracer, SpanKind

import otel_muzzle
from otel_muzzle import MuzzleSampler, _patch, install
from tests.helpers import LOUD_TRACE_ID, by_name, parent_id, remote_parent, sdk_tracer, span_id


def server_work_client(tracer: trace.Tracer, context: Context | None = None) -> None:
    """SERVER -> INTERNAL -> CLIENT under a sampled remote parent whose trace id the ratio never selects."""
    with (
        tracer.start_as_current_span("GET /", context=context or remote_parent(), kind=SpanKind.SERVER),
        tracer.start_as_current_span("work"),
        tracer.start_as_current_span("GET upstream", kind=SpanKind.CLIENT),
    ):
        pass


def assert_reparented(exporter: InMemorySpanExporter) -> None:
    spans = by_name(exporter)
    assert sorted(spans) == ["GET /", "GET upstream"]
    assert parent_id(spans["GET upstream"]) == span_id(spans["GET /"])


def sdk_sampler(tracer: Tracer) -> MuzzleSampler:
    assert isinstance(tracer.sampler, MuzzleSampler)
    return tracer.sampler


class TestInstall:
    def test_public_api(self) -> None:
        assert sorted(otel_muzzle.__all__) == ["MuzzleSampler", "__version__", "install"]

    def test_install_after_the_provider_is_set(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        trace.set_tracer_provider(provider())
        install()
        server_work_client(trace.get_tracer("app"))
        assert_reparented(exporter)

    def test_install_before_the_provider_is_set(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install()
        trace.set_tracer_provider(provider())
        server_work_client(trace.get_tracer("app"))
        assert_reparented(exporter)

    def test_install_reaches_tracers_created_before_it(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        tracer = sdk_tracer(provider(), "early")
        install(ratio=0.0)
        server_work_client(tracer)
        assert_reparented(exporter)

    def test_install_reaches_proxy_tracers_from_before_any_provider(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        tracer = trace.get_tracer("early")
        install()
        trace.set_tracer_provider(provider())
        server_work_client(tracer)
        assert_reparented(exporter)

    def test_install_wraps_each_tracers_sampler_when_it_first_starts_a_span(
        self,
        provider: Callable[..., TracerProvider],
    ) -> None:
        tracer_provider = provider()
        tracer = sdk_tracer(tracer_provider, "app")
        original = tracer.sampler
        install(ratio=0.25)
        assert tracer.sampler is original
        tracer.start_span("first").end()
        sampler = sdk_sampler(tracer)
        assert sampler.delegate is original
        assert sampler.ratio == 0.25
        tracer.start_span("second").end()
        assert tracer.sampler is sampler
        assert tracer_provider.sampler is original  # the provider's own attribute is left alone

    def test_install_wraps_the_sampler_the_environment_selected(
        self, monkeypatch: pytest.MonkeyPatch, provider: Callable[..., TracerProvider]
    ) -> None:
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", "traceidratio")
        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.5")
        tracer = sdk_tracer(provider(), "app")
        install(ratio=0.0)
        tracer.start_span("first").end()
        sampler = sdk_sampler(tracer)
        assert isinstance(sampler.delegate, TraceIdRatioBased)
        assert sampler.get_description() == "MuzzleSampler{ratio=0.0, delegate=TraceIdRatioBased{0.5}}"

    def test_a_tracer_that_already_has_a_muzzle_sampler_keeps_it(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        mine = MuzzleSampler(ParentBased(ALWAYS_ON), ratio=1.0)
        tracer = sdk_tracer(provider(sampler=mine), "app")
        install(ratio=0.0)
        server_work_client(tracer)
        assert tracer.sampler is mine
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream", "work"]  # its ratio 1.0 keeps everything

    def test_install_is_idempotent(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install()
        patched = Tracer.start_span
        install()
        assert Tracer.start_span is patched
        tracer = sdk_tracer(provider(), "app")
        server_work_client(tracer)
        assert not isinstance(sdk_sampler(tracer).delegate, MuzzleSampler)
        assert_reparented(exporter)

    def test_concurrent_first_spans_wrap_a_tracer_once(self, provider: Callable[..., TracerProvider]) -> None:
        tracer = sdk_tracer(provider(), "app")
        original = tracer.sampler
        install()
        barrier = threading.Barrier(8)

        def start() -> None:
            barrier.wait()
            tracer.start_span("span").end()

        threads = [threading.Thread(target=start) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sdk_sampler(tracer).delegate is original

    def test_install_uses_the_environment_ratio_by_default(
        self, monkeypatch: pytest.MonkeyPatch, provider: Callable[..., TracerProvider]
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "0.5")
        install()
        tracer = sdk_tracer(provider(), "app")
        tracer.start_span("first").end()
        assert sdk_sampler(tracer).ratio == 0.5

    def test_the_environment_ratio_is_read_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, provider: Callable[..., TracerProvider]
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "abc")
        with caplog.at_level(logging.WARNING, logger="otel_muzzle"):
            install()
            for name in ("a", "b", "c"):
                tracer = sdk_tracer(provider(), name)
                tracer.start_span("first").end()
                assert sdk_sampler(tracer).ratio == 0.1
        assert [record.getMessage() for record in caplog.records] == [
            'otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO must be a number within [0, 1], got "abc"; using 0.1'
        ]

    def test_an_explicit_ratio_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "0")
        install(ratio=1.0)
        server_work_client(sdk_tracer(provider(), "app"))
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream", "work"]

    def test_install_again_keeps_its_ratio_and_warns(
        self, caplog: pytest.LogCaptureFixture, provider: Callable[..., TracerProvider]
    ) -> None:
        install()
        with caplog.at_level(logging.WARNING, logger="otel_muzzle"):
            install(ratio=0.05)
        assert [record.getMessage() for record in caplog.records] == [
            "otel-muzzle: already installed with ratio 0.1, ignoring ratio 0.05; "
            "under opentelemetry-instrument, set OTEL_MUZZLE_VERBOSE_RATIO instead"
        ]
        assert caplog.records[0].levelno == logging.WARNING
        tracer = sdk_tracer(provider(), "app")
        tracer.start_span("first").end()
        assert sdk_sampler(tracer).ratio == 0.1

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="otel_muzzle"):
            install()
            install(ratio=0.1)
        assert caplog.records == []

    @pytest.mark.parametrize(("ratio", "error"), [(1.5, ValueError), (float("nan"), ValueError), ("0.5", TypeError)])
    def test_install_with_an_invalid_ratio_raises_and_does_nothing(self, ratio: Any, error: type[Exception]) -> None:
        with pytest.raises(error, match=r"ratio must be a number within \[0, 1\]"):
            install(ratio=ratio)
        assert not _patch.is_patched()

    def test_an_invalid_ratio_raises_even_when_already_installed(self) -> None:
        install()
        with pytest.raises(ValueError, match="ratio must be"):
            install(ratio=2.0)

    @pytest.mark.parametrize("value", ["true", "TRUE", " true "])
    def test_install_with_the_sdk_disabled_does_nothing(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("OTEL_SDK_DISABLED", value)
        install()
        assert not _patch.is_patched()
        assert _patch._ratio is None
        assert isinstance(TracerProvider(shutdown_on_exit=False).get_tracer("app"), NoOpTracer)

    @pytest.mark.parametrize("value", ["false", "", "1"])
    def test_other_sdk_disabled_values_install(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("OTEL_SDK_DISABLED", value)
        install()
        assert _patch.is_patched()

    def test_verbose_traces_keep_every_span(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        install(ratio=0.1)
        server_work_client(sdk_tracer(provider(), "app"), remote_parent(trace_id=LOUD_TRACE_ID))
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream", "work"]
        assert parent_id(spans["GET upstream"]) == span_id(spans["work"])


class TestUninstall:
    def test_uninstall_gives_each_tracer_its_own_sampler_back(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        tracer = sdk_tracer(provider(), "app")
        original = tracer.sampler
        install()
        tracer.start_span("first").end()
        assert isinstance(tracer.sampler, MuzzleSampler)
        _patch.uninstall()
        exporter.clear()
        server_work_client(tracer)
        assert tracer.sampler is original
        assert sorted(by_name(exporter)) == ["GET /", "GET upstream", "work"]

    def test_uninstall_keeps_the_patch_for_muzzle_samplers_the_application_set(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        mine = MuzzleSampler(ratio=0.0)
        tracer = sdk_tracer(provider(sampler=mine), "app")
        install()
        _patch.uninstall()
        assert _patch.is_patched()
        server_work_client(tracer)
        assert tracer.sampler is mine
        assert_reparented(exporter)

    def test_uninstall_mid_trace_keeps_the_trace_connected(
        self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter
    ) -> None:
        tracer = sdk_tracer(provider(), "app")
        install(ratio=0.0)
        with (
            tracer.start_as_current_span("GET /", context=remote_parent(), kind=SpanKind.SERVER),
            tracer.start_as_current_span("work"),
        ):
            _patch.uninstall()
            with (
                tracer.start_as_current_span("inner"),
                tracer.start_as_current_span("GET upstream", kind=SpanKind.CLIENT),
            ):
                pass
        spans = by_name(exporter)
        assert sorted(spans) == ["GET /", "GET upstream", "inner"]
        assert parent_id(spans["inner"]) == span_id(spans["GET /"])
        assert parent_id(spans["GET upstream"]) == span_id(spans["inner"])

    def test_install_after_uninstall_wraps_again_with_the_new_ratio(
        self, provider: Callable[..., TracerProvider]
    ) -> None:
        tracer = sdk_tracer(provider(), "app")
        install(ratio=0.1)
        tracer.start_span("first").end()
        _patch.uninstall()
        tracer.start_span("second").end()
        install(ratio=0.5)
        tracer.start_span("third").end()
        sampler = sdk_sampler(tracer)
        assert sampler.ratio == 0.5
        assert not isinstance(sampler.delegate, MuzzleSampler)

    def test_install_after_uninstall_rewraps_tracers_that_started_no_span_in_between(
        self,
        provider: Callable[..., TracerProvider],
    ) -> None:
        tracer = sdk_tracer(provider(), "app")
        original = tracer.sampler
        install(ratio=0.1)
        tracer.start_span("first").end()
        _patch.uninstall()
        install(ratio=0.5)
        tracer.start_span("second").end()
        sampler = sdk_sampler(tracer)
        assert sampler.ratio == 0.5
        assert sampler.delegate is original
