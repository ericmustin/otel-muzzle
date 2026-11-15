# SPDX-License-Identifier: Apache-2.0
"""The zero-code path, for real: a script run under opentelemetry-instrument in a fresh interpreter.

The script does no OpenTelemetry setup. opentelemetry-distro's configurator builds the provider from the environment
(OTEL_TRACES_SAMPLER included), and opentelemetry-instrument loads the `muzzle` entry point after it. Spans go to the
SDK's console exporter on stdout; the script writes what it saw of the SDK to a JSON report.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import (
    EXAMPLE_POLICY,
    LOUD_TRACE_ID,
    POLICY_DROP_TRACE_ID,
    POLICY_KEEP_TRACE_ID,
    QUIET_TRACE_ID,
    REMOTE_SPAN_ID,
)

INSTRUMENT = Path(sys.executable).with_name("opentelemetry-instrument")

SCRIPT = """
import json, os, sys
from importlib.metadata import version
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import Tracer
from opentelemetry.trace import SpanKind

carrier = {"traceparent": os.environ["TEST_TRACEPARENT"]}
tracer = trace.get_tracer("app")
with tracer.start_as_current_span("GET /", context=propagate.extract(carrier), kind=SpanKind.SERVER):
    with tracer.start_as_current_span("work"):
        with tracer.start_as_current_span("GET upstream", kind=SpanKind.CLIENT):
            pass

report = {
    "provider": type(trace.get_tracer_provider()).__module__ + "." + type(trace.get_tracer_provider()).__name__,
    "sampler": tracer.sampler.get_description() if hasattr(tracer, "sampler") else None,
    "patched": getattr(Tracer.start_span, "_otel_muzzle_patch", False),
    "imported": "otel_muzzle" in sys.modules,
    "sdk_version": version("opentelemetry-sdk"),
}
with open(os.environ["TEST_REPORT"], "w") as f:
    json.dump(report, f)
"""

PARENT_BASED_RATIO_HALF = (
    "ParentBased{root:TraceIdRatioBased{0.5},remoteParentSampled:AlwaysOnSampler,"
    "remoteParentNotSampled:AlwaysOffSampler,localParentSampled:AlwaysOnSampler,"
    "localParentNotSampled:AlwaysOffSampler}"
)


def console_spans(stdout: str) -> dict[str, dict[str, Any]]:
    """The ConsoleSpanExporter's output, one indented JSON object per span, by span name."""
    decoder, index = json.JSONDecoder(), 0
    spans: dict[str, dict[str, Any]] = {}
    while True:
        while index < len(stdout) and stdout[index].isspace():
            index += 1
        if index == len(stdout):
            return spans
        span, index = decoder.raw_decode(stdout, index)
        spans[span["name"]] = span


def run(tmp_path: Path, trace_id: int = QUIET_TRACE_ID, **env: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Runs the script under opentelemetry-instrument; returns (report, spans by name, stderr)."""
    script, report = tmp_path / "app.py", tmp_path / "report.json"
    script.write_text(SCRIPT)
    base = {name: value for name, value in os.environ.items() if not name.startswith("OTEL_")}
    full_env = {
        **base,
        "OTEL_TRACES_EXPORTER": "console",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_BSP_SCHEDULE_DELAY": "10",
        "TEST_TRACEPARENT": f"00-{trace_id:032x}-{REMOTE_SPAN_ID:016x}-01",
        "TEST_REPORT": str(report),
        **env,
    }
    result = subprocess.run(
        [str(INSTRUMENT), sys.executable, str(script)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(report.read_text()), console_spans(result.stdout), result.stderr


def assert_reparented(spans: dict[str, dict[str, Any]]) -> None:
    assert sorted(spans) == ["GET /", "GET upstream"]
    assert spans["GET /"]["parent_id"] == f"0x{REMOTE_SPAN_ID:016x}"
    assert spans["GET upstream"]["parent_id"] == spans["GET /"]["context"]["span_id"]
    assert spans["GET upstream"]["context"]["trace_id"] == spans["GET /"]["context"]["trace_id"]


@pytest.fixture(autouse=True)
def _needs_opentelemetry_instrument() -> None:
    assert INSTRUMENT.exists(), f"{INSTRUMENT} is missing: install the test extra"


class TestZeroCode:
    def test_opentelemetry_instrument_installs_muzzle_around_the_env_sampler(self, tmp_path: Path) -> None:
        report, spans, stderr = run(
            tmp_path, OTEL_TRACES_SAMPLER="parentbased_traceidratio", OTEL_TRACES_SAMPLER_ARG="0.5"
        )
        assert report["provider"] == "opentelemetry.sdk.trace.TracerProvider"
        assert report["patched"] is True
        assert report["sampler"] == f"MuzzleSampler{{ratio=0.1, delegate={PARENT_BASED_RATIO_HALF}}}"
        assert_reparented(spans)
        assert "otel-muzzle" not in stderr

    def test_the_default_delegate_is_the_sdks_default_sampler(self, tmp_path: Path) -> None:
        report, spans, _ = run(tmp_path)
        assert report["sampler"].startswith("MuzzleSampler{ratio=0.1, delegate=ParentBased{root:AlwaysOnSampler,")
        assert_reparented(spans)

    def test_the_environment_ratio_reaches_zero_code(self, tmp_path: Path) -> None:
        report, spans, _ = run(tmp_path, OTEL_MUZZLE_VERBOSE_RATIO="1")
        assert report["sampler"].startswith("MuzzleSampler{ratio=1.0, ")
        assert sorted(spans) == ["GET /", "GET upstream", "work"]
        assert spans["GET upstream"]["parent_id"] == spans["work"]["context"]["span_id"]

    def test_a_trace_id_the_ratio_selects_keeps_every_span(self, tmp_path: Path) -> None:
        _, spans, _ = run(tmp_path, trace_id=LOUD_TRACE_ID)
        assert sorted(spans) == ["GET /", "GET upstream", "work"]

    def test_an_invalid_environment_ratio_is_logged_once(self, tmp_path: Path) -> None:
        report, spans, stderr = run(tmp_path, OTEL_MUZZLE_VERBOSE_RATIO="lots")
        assert report["sampler"].startswith("MuzzleSampler{ratio=0.1, ")
        assert stderr.count('otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO must be a number within [0, 1], got "lots"') == 1
        assert_reparented(spans)

    def test_the_policy_file_reaches_zero_code(self, tmp_path: Path) -> None:
        report, spans, stderr = run(
            tmp_path, trace_id=POLICY_DROP_TRACE_ID, OTEL_MUZZLE_POLICY_FILE=str(EXAMPLE_POLICY)
        )
        assert report["sampler"].startswith("MuzzleSampler{mode=policy, delegate=ParentBased{root:AlwaysOnSampler,")
        assert_reparented(spans)
        assert "otel-muzzle" not in stderr
        _, spans, _ = run(tmp_path, trace_id=POLICY_KEEP_TRACE_ID, OTEL_MUZZLE_POLICY_FILE=str(EXAMPLE_POLICY))
        assert sorted(spans) == ["GET /", "GET upstream", "work"]

    def test_an_unreadable_policy_file_is_logged_once_and_drops_nothing(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.json"
        report, spans, stderr = run(tmp_path, trace_id=POLICY_DROP_TRACE_ID, OTEL_MUZZLE_POLICY_FILE=str(missing))
        assert report["sampler"].startswith("MuzzleSampler{mode=policy, ")
        assert sorted(spans) == ["GET /", "GET upstream", "work"]
        assert stderr.count(f"otel-muzzle: cannot load the policy file {missing}") == 1

    @pytest.mark.parametrize("disabled", ["muzzle", "flask, muzzle"])
    def test_disabled_instrumentations_turns_it_off(self, tmp_path: Path, disabled: str) -> None:
        report, spans, _ = run(tmp_path, OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=disabled)
        assert not report["sampler"].startswith("MuzzleSampler")
        assert sorted(spans) == ["GET /", "GET upstream", "work"]

    def test_the_sdk_disabled_turns_it_off(self, tmp_path: Path) -> None:
        report, spans, _ = run(tmp_path, OTEL_SDK_DISABLED="true")
        assert report["patched"] is False
        assert report["sampler"] is None  # the SDK hands out no-op tracers
        assert spans == {}

    def test_opentelemetry_instrument_skips_muzzle_when_the_sdk_is_out_of_range(self, tmp_path: Path) -> None:
        # The Operator's layout: another opentelemetry-sdk distribution earlier on sys.path than the one pip installed
        # next to otel-muzzle. Here it is a 2.0.0 dist-info with the real SDK's entry points and no code, so the real
        # SDK still runs, but importlib.metadata reports 2.0.0.
        fake = tmp_path / "shadow" / "opentelemetry_sdk-2.0.0.dist-info"
        fake.mkdir(parents=True)
        (fake / "METADATA").write_text("Metadata-Version: 2.1\nName: opentelemetry-sdk\nVersion: 2.0.0\n")
        (fake / "entry_points.txt").write_text(distribution("opentelemetry-sdk").read_text("entry_points.txt") or "")
        report, spans, _ = run(tmp_path, PYTHONPATH=str(fake.parent))
        assert report["sdk_version"] == "2.0.0"
        assert report["patched"] is False
        assert report["imported"] is False
        assert not report["sampler"].startswith("MuzzleSampler")
        assert sorted(spans) == ["GET /", "GET upstream", "work"]
