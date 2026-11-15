# SPDX-License-Identifier: Apache-2.0
"""Policy mode: compiling span sampling policies and deciding with them."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from opentelemetry.trace import INVALID_SPAN_CONTEXT, SpanContext, SpanKind, TraceFlags, TraceState

from otel_muzzle.policy.parser import parse_policy_set
from otel_muzzle.policy.span_sampling import SpanSamplingImplementer
from tests.helpers import (
    EXAMPLE_POLICY,
    LOCAL_SPAN_ID,
    POLICY_DROP_TRACE_ID,
    POLICY_KEEP_TRACE_ID,
    policy_vectors,
)

LOCAL = SpanContext(POLICY_DROP_TRACE_ID, LOCAL_SPAN_ID, False, TraceFlags(TraceFlags.SAMPLED))
KIND_IS_INTERNAL = {
    "target": {"recordField": "SPAN_RECORD_FIELD_KIND"},
    "predicate": {"equals": {"stringValue": "INTERNAL"}},
}


def implementer(*policies: dict[str, Any]) -> SpanSamplingImplementer:
    out = SpanSamplingImplementer()
    out.apply_policies(parse_policy_set(json.dumps({"policies": list(policies)})))
    return out


def sampling(policy_id: str, percentage: float, *matches: dict[str, Any], **config: Any) -> dict[str, Any]:
    return {
        "id": policy_id,
        "spanSampling": {"matches": list(matches), "sampling": {"percentage": percentage, **config}},
    }


def keep(
    policies: SpanSamplingImplementer,
    trace_id: int = POLICY_DROP_TRACE_ID,
    *,
    kind: SpanKind | None = SpanKind.INTERNAL,
    parent: SpanContext = LOCAL,
    trace_state: TraceState | None = None,
    attributes: dict[str, Any] | None = None,
    name: str = "work",
) -> bool:
    return policies.keep(trace_id, name, kind, parent, trace_state, attributes)


class TestSpanSampling:
    def test_statuses_match_the_shared_vectors(self) -> None:
        for name, policy_set in policy_vectors("decision")["policy_sets"].items():
            if policy_set["malformed"]:
                continue
            statuses = SpanSamplingImplementer().apply_policies(parse_policy_set(policy_set["text"]))
            got = [{"id": status.policy_id, "applied": status.applied} for status in statuses]
            assert got == policy_set["statuses"], name
            assert all(status.error for status in statuses if not status.applied), name
            assert all(status.error is None for status in statuses if status.applied), name

    def test_without_policies_every_span_is_kept(self) -> None:
        assert keep(SpanSamplingImplementer())
        assert not SpanSamplingImplementer().active

    def test_the_example_policy_drops_internal_spans_under_a_parent(self) -> None:
        policies = SpanSamplingImplementer()
        policies.apply_policies(parse_policy_set(EXAMPLE_POLICY.read_text()))
        assert policies.active
        assert not keep(policies)
        assert keep(policies, POLICY_KEEP_TRACE_ID)
        assert keep(policies, kind=SpanKind.SERVER)
        assert keep(policies, parent=INVALID_SPAN_CONTEXT)
        assert keep(policies, trace_state=TraceState([("muzzle", "verbose")]))

    def test_the_most_restrictive_policy_wins(self) -> None:
        policies = implementer(sampling("all", 100), sampling("half", 50), sampling("none", 0, KIND_IS_INTERNAL))
        assert not keep(policies, POLICY_KEEP_TRACE_ID)
        assert keep(policies, POLICY_KEEP_TRACE_ID, kind=SpanKind.CLIENT)
        assert not keep(policies, POLICY_DROP_TRACE_ID, kind=SpanKind.CLIENT)

    def test_a_policy_with_an_unsupported_selector_is_inert(self, caplog: pytest.LogCaptureFixture) -> None:
        resource = {"target": {"resourceAttribute": {"path": ["service.name"]}}, "predicate": {"exists": {}}}
        policies = SpanSamplingImplementer()
        with caplog.at_level(logging.WARNING):
            (status,) = policies.apply_policies(
                parse_policy_set(json.dumps({"policies": [sampling("by-resource", 0, KIND_IS_INTERNAL, resource)]}))
            )
        assert not status.applied and status.error is not None
        assert "matches[1].target" in status.error and "resource_attribute" in status.error
        assert keep(policies)
        assert not policies.active
        (record,) = caplog.records
        assert "by-resource" in record.getMessage() and "resource_attribute" in record.getMessage()

    def test_a_disabled_policy_is_applied_and_absent(self) -> None:
        policies = SpanSamplingImplementer()
        (status,) = policies.apply_policies(
            parse_policy_set(json.dumps({"policies": [{**sampling("off", 0), "enabled": False}]}))
        )
        assert status.applied and status.error is None
        assert keep(policies)

    def test_a_runtime_error_keeps_the_span(self, caplog: pytest.LogCaptureFixture) -> None:
        class Explosive(dict[str, Any]):
            def __contains__(self, key: object) -> bool:
                raise RuntimeError("boom")

        attribute = {"target": {"spanAttribute": {"path": ["a"]}}, "predicate": {"exists": {}}}
        policies = implementer(sampling("fragile", 0, attribute))
        with caplog.at_level(logging.WARNING):
            assert keep(policies, attributes=Explosive())
            assert keep(policies, attributes=Explosive())
        (record,) = caplog.records
        assert "fragile" in record.getMessage() and "boom" in record.getMessage()
        assert not keep(policies, attributes={"a": 1})

    def test_an_inconsistent_arriving_threshold_is_ignored(self) -> None:
        policies = implementer(sampling("all", 100))
        assert keep(policies, POLICY_DROP_TRACE_ID, trace_state=TraceState([("ot", "th:8")]))

    def test_rv_replaces_the_trace_id(self) -> None:
        policies = implementer(sampling("half", 50))
        assert not keep(policies, POLICY_KEEP_TRACE_ID, trace_state=TraceState([("ot", "rv:00000000000000")]))
        assert keep(policies, POLICY_DROP_TRACE_ID, trace_state=TraceState([("ot", "rv:ffffffffffffff")]))

    def test_apply_policies_replaces_the_whole_set(self) -> None:
        policies = implementer(sampling("none", 0))
        assert not keep(policies, POLICY_KEEP_TRACE_ID)
        policies.apply_policies([])
        assert keep(policies, POLICY_KEEP_TRACE_ID)
