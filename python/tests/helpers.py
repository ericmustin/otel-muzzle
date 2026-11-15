# SPDX-License-Identifier: Apache-2.0
"""Constants and helpers shared by the tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Tracer, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

# Trace-id bytes 12-15 are 0xffffffff: never verbose by ratio unless the ratio is 1.
QUIET_TRACE_ID = 0x4BF92F3577B34DA6A3CE929DFFFFFFFF
# Trace-id bytes 12-15 are 0: verbose by ratio whenever the ratio is above 0.
LOUD_TRACE_ID = 0x4BF92F3577B34DA6A3CE929D00000000
REMOTE_SPAN_ID = 0x00F067AA0BA902B7
LOCAL_SPAN_ID = 0x53995C3F42CD8AD8

PACKAGE_DIR = Path(__file__).resolve().parent.parent
RATIO_VECTORS = PACKAGE_DIR.parent / "testdata" / "ratio_vectors.json"
RATIO_ENV_VECTORS = PACKAGE_DIR.parent / "testdata" / "ratio_env_vectors.json"
EXAMPLE_POLICY = PACKAGE_DIR.parent / "examples" / "policies" / "muzzle.json"
# Policy mode reads trace-id bytes 9-15. All ones is kept by any percentage above 0, and zero by none below 100.
POLICY_KEEP_TRACE_ID = 0x4BF92F3577B34DA6A3FFFFFFFFFFFFFF
POLICY_DROP_TRACE_ID = 0x4BF92F3577B34DA6A300000000000000
VERBOSE = TraceState([("muzzle", "verbose")])
VENDOR = TraceState([("vendor", "x")])


def ratio_vectors() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = json.loads(RATIO_VECTORS.read_text())["cases"]
    return cases


def ratio_env_vectors() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = json.loads(RATIO_ENV_VECTORS.read_text())["cases"]
    return cases


def policy_vectors(name: str) -> dict[str, Any]:
    """testdata/policy_<name>_vectors.json: threshold, tracestate, regex or decision."""
    doc: dict[str, Any] = json.loads((PACKAGE_DIR.parent / "testdata" / f"policy_{name}_vectors.json").read_text())
    return doc


def flags(sampled: bool) -> TraceFlags:
    return TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT)


def remote_parent(
    trace_id: int = QUIET_TRACE_ID, sampled: bool = True, trace_state: TraceState | None = None
) -> Context:
    """A context whose current span is a remote parent, as a W3C extract would produce."""
    span_context = SpanContext(trace_id, REMOTE_SPAN_ID, True, flags(sampled), trace_state)
    return trace.set_span_in_context(NonRecordingSpan(span_context), Context())


def local_parent(
    trace_id: int = QUIET_TRACE_ID, sampled: bool = True, trace_state: TraceState | None = None
) -> Context:
    """A context whose current span is a local span with the given context."""
    span_context = SpanContext(trace_id, LOCAL_SPAN_ID, False, flags(sampled), trace_state)
    return trace.set_span_in_context(NonRecordingSpan(span_context), Context())


def by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    """Exported spans by name."""
    return {span.name: span for span in exporter.get_finished_spans()}


def parent_id(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


def span_id(span: ReadableSpan | trace.Span) -> int:
    context = span.get_span_context() if isinstance(span, trace.Span) else span.context
    assert context is not None
    return context.span_id


def sdk_tracer(provider: TracerProvider, name: str = "test") -> Tracer:
    """The provider's tracer, narrowed to the SDK class so that tests can read its ``sampler``."""
    tracer = provider.get_tracer(name)
    assert isinstance(tracer, Tracer)
    return tracer
