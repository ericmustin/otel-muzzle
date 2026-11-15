# SPDX-License-Identifier: Apache-2.0
"""Policy mode: the store and the file provider."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags

from otel_muzzle.policy import _runtime
from otel_muzzle.policy.parser import parse_policy_set
from otel_muzzle.policy.provider import FilePolicyProvider
from otel_muzzle.policy.span_sampling import SpanSamplingImplementer
from otel_muzzle.policy.store import PolicyStore
from tests.helpers import EXAMPLE_POLICY, LOCAL_SPAN_ID, POLICY_DROP_TRACE_ID

LOCAL = SpanContext(POLICY_DROP_TRACE_ID, LOCAL_SPAN_ID, False, TraceFlags(TraceFlags.SAMPLED))
DROP_ALL = '{"policies": [{"id": "none", "spanSampling": {"sampling": {"percentage": 0}}}]}'
KEEP_ALL = '{"policies": [{"id": "all", "spanSampling": {"sampling": {"percentage": 100}}}]}'


def kept(policies: SpanSamplingImplementer) -> bool:
    return policies.keep(POLICY_DROP_TRACE_ID, "work", SpanKind.INTERNAL, LOCAL, None, None)


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    shutil.copy(EXAMPLE_POLICY, path)
    return path


class TestStore:
    def test_set_policies_replaces_the_whole_set(self) -> None:
        policies = SpanSamplingImplementer()
        store = PolicyStore(policies)
        assert list(store.statuses) == []
        store.set_policies(parse_policy_set(DROP_ALL))
        assert [(status.policy_id, status.applied) for status in store.statuses] == [("none", True)]
        assert not kept(policies)
        store.set_policies(parse_policy_set(KEEP_ALL))
        assert [status.policy_id for status in store.statuses] == ["all"]
        assert kept(policies)


class TestFileProvider:
    def test_start_loads_the_file(self, policy_file: Path) -> None:
        policies = SpanSamplingImplementer()
        store = PolicyStore(policies)
        FilePolicyProvider(policy_file, store).start()
        assert [status.policy_id for status in store.statuses] == ["drop-internal-spans"]
        assert not kept(policies)

    @pytest.mark.parametrize("content", [None, "", "{", "[]", b"\xff\xfe"])
    def test_an_unreadable_file_drops_nothing_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str | bytes | None
    ) -> None:
        path = tmp_path / "policy.json"
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif content is not None:
            path.write_text(content)
        policies = SpanSamplingImplementer()
        store = PolicyStore(policies)
        with caplog.at_level(logging.WARNING):
            FilePolicyProvider(path, store).start()
        assert kept(policies) and list(store.statuses) == []
        (record,) = caplog.records
        assert str(path) in record.getMessage() and "no policy is in effect" in record.getMessage()

    def test_an_unexpected_error_while_loading_fails_open(
        self, policy_file: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(text: str) -> None:
            raise RuntimeError("boom")

        policies = SpanSamplingImplementer()
        provider = FilePolicyProvider(policy_file, PolicyStore(policies))
        provider.start()
        monkeypatch.setattr("otel_muzzle.policy.provider.parse_policy_set", boom)
        with caplog.at_level(logging.WARNING):
            assert provider.reload() is False
        assert not kept(policies)
        assert "boom; keeping the previous policy set" in caplog.records[-1].getMessage()

    def test_reload_swaps_the_set(self, policy_file: Path) -> None:
        policies = SpanSamplingImplementer()
        provider = FilePolicyProvider(policy_file, PolicyStore(policies))
        provider.start()
        assert not kept(policies)
        policy_file.write_text(KEEP_ALL)
        assert provider.reload() is True
        assert kept(policies)

    def test_a_malformed_reload_keeps_the_previous_set(
        self, policy_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        policies = SpanSamplingImplementer()
        store = PolicyStore(policies)
        provider = FilePolicyProvider(policy_file, store)
        provider.start()
        policy_file.write_text("{")
        with caplog.at_level(logging.WARNING):
            assert provider.reload() is False
        assert not kept(policies)
        assert [status.policy_id for status in store.statuses] == ["drop-internal-spans"]
        assert "keeping the previous policy set" in caplog.records[-1].getMessage()
        policy_file.unlink()
        assert provider.reload() is False
        assert not kept(policies)


class TestRuntime:
    def test_one_runtime_per_file(self, policy_file: Path, tmp_path: Path) -> None:
        first = _runtime.from_file(str(policy_file))
        assert _runtime.from_file(str(policy_file)) is first
        assert _runtime.from_file(f"{policy_file.parent}/./{policy_file.name}") is first
        other = tmp_path / "other.json"
        other.write_text(KEEP_ALL)
        assert _runtime.from_file(str(other)) is not first
        assert not kept(first)

    def test_the_file_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _runtime.policy_file_from_env() is None
        monkeypatch.setenv("OTEL_MUZZLE_POLICY_FILE", "  ")
        assert _runtime.policy_file_from_env() is None
        monkeypatch.setenv("OTEL_MUZZLE_POLICY_FILE", " /etc/policy.json ")
        assert _runtime.policy_file_from_env() == "/etc/policy.json"

    def test_the_environment_ratio_is_ignored_with_a_warning(
        self, policy_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("OTEL_MUZZLE_VERBOSE_RATIO", "0.5")
        with caplog.at_level(logging.WARNING):
            _runtime.from_file(str(policy_file))
            _runtime.from_file(str(policy_file))
        (record,) = caplog.records
        assert "OTEL_MUZZLE_VERBOSE_RATIO" in record.getMessage() and "ignored" in record.getMessage()

    def test_reload_reaches_the_provider(self, policy_file: Path) -> None:
        policies = _runtime.from_file(str(policy_file))
        policy_file.write_text(KEEP_ALL)
        assert _runtime.provider_for(policies).reload() is True
        assert kept(policies)


class TestReload:
    def test_reload_reads_every_loaded_file_again(self, policy_file: Path, tmp_path: Path) -> None:
        from otel_muzzle import policy

        other = tmp_path / "other.json"
        other.write_text(DROP_ALL)
        first, second = _runtime.from_file(str(policy_file)), _runtime.from_file(str(other))
        assert policy.reload() is True
        policy_file.write_text(KEEP_ALL)
        other.write_text(KEEP_ALL)
        assert policy.reload() is True
        assert kept(first) and kept(second)

    def test_reload_reports_a_file_that_failed(self, policy_file: Path) -> None:
        from otel_muzzle import policy

        policies = _runtime.from_file(str(policy_file))
        policy_file.write_text("{")
        assert policy.reload() is False
        assert not kept(policies)

    def test_reload_without_a_policy_file_does_nothing(self) -> None:
        from otel_muzzle import policy

        assert policy.reload() is True
