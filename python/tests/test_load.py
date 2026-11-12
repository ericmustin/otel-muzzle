# SPDX-License-Identifier: Apache-2.0
"""What loading otel_muzzle does, in a fresh interpreter each time.

opentelemetry-instrumentation is not a dependency, so `import otel_muzzle` and install() must work without it. A
meta-path hook makes it unimportable, whether or not this environment has it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

BLOCK_INSTRUMENTATION = """
import sys

class Block:
    def find_spec(self, name, path=None, target=None):
        if name == "opentelemetry.instrumentation" or name.startswith("opentelemetry.instrumentation."):
            raise ModuleNotFoundError(f"No module named {name!r} (hidden by the test)", name=name)
        return None

sys.meta_path.insert(0, Block())
"""


def run_python(script: str) -> dict[str, object]:
    """Runs the script in a fresh interpreter and returns the JSON it prints last."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    loaded: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    return loaded


class TestLoad:
    def test_importing_changes_nothing(self) -> None:
        report = run_python(
            """
            import json, sys
            from opentelemetry.sdk.trace import Tracer
            sdk_start_span = Tracer.start_span
            import otel_muzzle
            print(json.dumps({
                "unpatched": Tracer.start_span is sdk_start_span,
                "instrumentation": sorted(m for m in sys.modules if m.startswith("opentelemetry.instrumentation")),
            }))
            """
        )
        assert report == {"unpatched": True, "instrumentation": []}

    def test_without_opentelemetry_instrumentation_install_still_works(self) -> None:
        report = run_python(
            BLOCK_INSTRUMENTATION
            + """
import json
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

import otel_muzzle

otel_muzzle.install(ratio=0.0)
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("app")
with tracer.start_as_current_span("GET /", kind=SpanKind.SERVER):
    with tracer.start_as_current_span("work"):
        with tracer.start_as_current_span("GET upstream", kind=SpanKind.CLIENT):
            pass
spans = {s.name: s for s in exporter.get_finished_spans()}

try:
    import otel_muzzle.instrumentor
    instrumentor_error = None
except ImportError as error:
    instrumentor_error = str(error)

print(json.dumps({
    "exported": sorted(spans),
    "reparented": spans["GET upstream"].parent.span_id == spans["GET /"].context.span_id,
    "instrumentor_error": instrumentor_error,
}))
"""
        )
        assert report == {
            "exported": ["GET /", "GET upstream"],
            "reparented": True,
            "instrumentor_error": "No module named 'opentelemetry.instrumentation' (hidden by the test)",
        }
