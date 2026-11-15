# SPDX-License-Identifier: Apache-2.0
"""MuzzleInstrumentor in process, and the package metadata it is found through."""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import distribution, entry_points, requires

import pytest
from opentelemetry.instrumentation import dependencies
from opentelemetry.instrumentation.dependencies import get_dist_dependency_conflicts
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Sampler
from opentelemetry.trace import SpanKind
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

import otel_muzzle
from otel_muzzle import MuzzleSampler, _patch
from otel_muzzle.instrumentor import MuzzleInstrumentor
from otel_muzzle.package import _instruments
from tests.helpers import EXAMPLE_POLICY, by_name, remote_parent, sdk_tracer


@pytest.fixture(autouse=True)
def _fresh_instrumentor(monkeypatch: pytest.MonkeyPatch) -> None:
    # BaseInstrumentor is a per-class singleton that remembers whether it instrumented.
    monkeypatch.setattr(MuzzleInstrumentor, "_instance", None)


def started_sampler(tracer_provider: TracerProvider) -> Sampler:
    tracer = sdk_tracer(tracer_provider, "app")
    tracer.start_span("first").end()
    return tracer.sampler


class TestInstrumentor:
    def test_instrumentation_dependencies_match_the_instruments_extra(self) -> None:
        assert tuple(MuzzleInstrumentor().instrumentation_dependencies()) == _instruments
        requirements = [Requirement(r) for r in requires("otel-muzzle") or []]
        extra = [r for r in requirements if r.marker and r.marker.evaluate({"extra": "instruments"})]
        assert {(r.name, r.specifier) for r in extra} == {(r.name, r.specifier) for r in map(Requirement, _instruments)}

    def test_instrument_with_an_sdk_outside_the_range_does_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        real_version = dependencies.version
        monkeypatch.setattr(
            dependencies, "version", lambda name: "2.0.0" if name == "opentelemetry-sdk" else real_version(name)
        )
        with caplog.at_level(logging.ERROR, logger="opentelemetry.instrumentation.instrumentor"):
            MuzzleInstrumentor().instrument()
        assert not _patch.is_patched()
        assert any("opentelemetry-sdk" in record.getMessage() for record in caplog.records)

    def test_instrument_with_a_ratio_when_already_instrumented_changes_nothing(
        self, provider: Callable[..., TracerProvider], caplog: pytest.LogCaptureFixture
    ) -> None:
        # What happens under opentelemetry-instrument, which has already called instrument(): BaseInstrumentor returns
        # before _instrument, so the ratio is not even validated, and otel-muzzle's ignored-ratio warning never fires.
        MuzzleInstrumentor().instrument()
        with caplog.at_level(logging.WARNING):
            MuzzleInstrumentor().instrument(ratio=0.5)
            MuzzleInstrumentor().instrument(ratio="not a number")
        sampler = started_sampler(provider())
        assert isinstance(sampler, MuzzleSampler)
        assert sampler.ratio == 0.1
        assert {record.name for record in caplog.records} == {"opentelemetry.instrumentation.instrumentor"}
        assert {record.getMessage() for record in caplog.records} == {
            "Attempting to instrument while already instrumented"
        }

    def test_uninstrument_after_a_direct_install_changes_nothing(self, provider: Callable[..., TracerProvider]) -> None:
        # BaseInstrumentor only uninstruments what its own instrument() installed.
        otel_muzzle.install()
        MuzzleInstrumentor().uninstrument()
        assert _patch._ratio == 0.1
        assert isinstance(started_sampler(provider()), MuzzleSampler)

    def test_instrument_installs(self, provider: Callable[..., TracerProvider], exporter: InMemorySpanExporter) -> None:
        MuzzleInstrumentor().instrument()
        assert _patch.is_patched()
        tracer = provider().get_tracer("app")
        with (
            tracer.start_as_current_span("GET /", context=remote_parent(), kind=SpanKind.SERVER),
            tracer.start_as_current_span("work"),
        ):
            pass
        assert list(by_name(exporter)) == ["GET /"]

    def test_instrument_passes_the_ratio_on(self, provider: Callable[..., TracerProvider]) -> None:
        MuzzleInstrumentor().instrument(ratio=0.25)
        sampler = started_sampler(provider())
        assert isinstance(sampler, MuzzleSampler)
        assert sampler.ratio == 0.25

    def test_instrument_reads_the_ratio_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, provider: Callable[..., TracerProvider]
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "0.5")
        MuzzleInstrumentor().instrument(skip_dep_check=True)
        sampler = started_sampler(provider())
        assert isinstance(sampler, MuzzleSampler)
        assert sampler.ratio == 0.5

    def test_instrument_passes_the_policy_file_on(self, provider: Callable[..., TracerProvider]) -> None:
        MuzzleInstrumentor().instrument(policy_file=str(EXAMPLE_POLICY))
        sampler = started_sampler(provider())
        assert isinstance(sampler, MuzzleSampler)
        assert sampler.ratio is None and sampler.policies is not None and sampler.policies.active

    def test_uninstrument_stops_wrapping_and_keeps_the_patch(self, provider: Callable[..., TracerProvider]) -> None:
        tracer_provider = provider()
        MuzzleInstrumentor().instrument()
        assert isinstance(started_sampler(tracer_provider), MuzzleSampler)
        MuzzleInstrumentor().uninstrument()
        assert started_sampler(tracer_provider) is tracer_provider.sampler
        assert _patch.is_patched()
        MuzzleInstrumentor().instrument()
        assert isinstance(started_sampler(tracer_provider), MuzzleSampler)

    def test_instrument_with_the_sdk_disabled_does_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        MuzzleInstrumentor().instrument()
        assert not _patch.is_patched()


class TestMetadata:
    def test_the_entry_point_is_registered_and_loads_the_instrumentor(self) -> None:
        (entry_point,) = entry_points(group="opentelemetry_instrumentor", name="muzzle")
        assert entry_point.value == "otel_muzzle.instrumentor:MuzzleInstrumentor"
        assert entry_point.load() is MuzzleInstrumentor

    def test_opentelemetry_instrument_sees_no_dependency_conflict(self) -> None:
        assert get_dist_dependency_conflicts(distribution("otel-muzzle")) is None

    def test_the_distribution_declares_the_sdk_range_for_opentelemetry_instrument(self) -> None:
        # opentelemetry-instrument reads requirements marked extra == "instruments" and skips an entry point whose
        # requirement the environment does not meet. It is the SDK check that holds even after pip install --no-deps.
        requirements = [Requirement(req) for req in distribution("otel-muzzle").requires or []]
        instruments = [req for req in requirements if req.marker and req.marker.evaluate({"extra": "instruments"})]
        assert [(req.name, req.specifier) for req in instruments] == [("opentelemetry-sdk", SpecifierSet(">=1.30,<2"))]

    def test_the_distribution_depends_on_the_api_and_sdk_only(self) -> None:
        dist = distribution("otel-muzzle")
        runtime = sorted(req for req in dist.requires or [] if "extra ==" not in req)
        assert [req.replace(" ", "") for req in runtime] == ["opentelemetry-api<2,>=1.30", "opentelemetry-sdk<2,>=1.30"]
        assert dist.version == otel_muzzle.__version__
