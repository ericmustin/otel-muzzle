# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures.

install() patches opentelemetry.sdk.trace.Tracer at class level and keeps its state in otel_muzzle._patch, and the
global tracer provider can only be set once per process. The autouse fixture below resets both around every test.
The zero-code tests run in subprocesses instead (test_zero_code.py, test_load.py).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.test.globals_test import reset_trace_globals

from otel_muzzle import _patch
from otel_muzzle.policy import _runtime

MUZZLE_ENV = (
    "OTEL_MUZZLE_POLICY_FILE",
    "OTEL_MUZZLE_VERBOSE_RATIO",
    "OTEL_SDK_DISABLED",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
)


@pytest.fixture(autouse=True)
def _isolate_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts unpatched with no global provider and no otel-muzzle environment, and ends that way."""
    assert not _patch.is_patched(), "a previous test leaked the class-level patch"
    for name in MUZZLE_ENV:
        monkeypatch.delenv(name, raising=False)
    # uninstall() leaves the class-level patch in place by design, so put the SDK's own start_span back.
    monkeypatch.setattr(Tracer, "start_span", Tracer.start_span)
    reset_trace_globals()
    _runtime.reset()
    yield
    _patch.uninstall()
    _runtime.reset()
    reset_trace_globals()


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> Callable[..., TracerProvider]:
    """Builds an SDK provider wired to the test's in-memory exporter."""

    def build(**kwargs: Any) -> TracerProvider:
        tracer_provider = TracerProvider(shutdown_on_exit=False, **kwargs)
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        return tracer_provider

    return build
