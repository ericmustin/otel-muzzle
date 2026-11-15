# SPDX-License-Identifier: Apache-2.0
"""Policy mode: reading a policy set from Protobuf JSON."""

from __future__ import annotations

import json
from typing import Any

import pytest

from otel_muzzle.policy.model import (
    Compare,
    Contains,
    Equals,
    Exists,
    InvalidPolicy,
    Policy,
    RecordField,
    Regex,
    SamplingMode,
    SpanAttribute,
    SpanRecordField,
    UnsupportedTarget,
)
from otel_muzzle.policy.parser import PolicyDocumentError, parse_policy_set
from tests.helpers import EXAMPLE_POLICY, policy_vectors

KIND = {"recordField": "SPAN_RECORD_FIELD_KIND"}


def parse_one(span_sampling: dict[str, Any], **envelope: Any) -> Policy | InvalidPolicy:
    (entry,) = parse_policy_set(json.dumps({"policies": [{"id": "p", **envelope, "spanSampling": span_sampling}]}))
    return entry


def error_of(span_sampling: dict[str, Any]) -> str:
    entry = parse_one(span_sampling)
    assert isinstance(entry, InvalidPolicy), entry
    return entry.error


class TestParser:
    def test_reads_the_example_policy(self) -> None:
        (policy,) = parse_policy_set(EXAMPLE_POLICY.read_text())
        assert isinstance(policy, Policy)
        assert policy.id == "drop-internal-spans" and policy.enabled
        assert policy.span_sampling is not None
        kind, parent, tracestate = policy.span_sampling.matches
        assert kind.target == RecordField(SpanRecordField.KIND) and kind.predicate == Equals("INTERNAL")
        assert not kind.negate
        assert parent.target == RecordField(SpanRecordField.PARENT_SPAN_ID) and parent.predicate == Exists()
        assert tracestate.predicate == Regex(r"(^|,)\s*muzzle=verbose\s*(,|$)") and tracestate.negate
        sampling = policy.span_sampling.sampling
        assert (sampling.percentage, sampling.mode, sampling.precision) == (10.0, SamplingMode.PROPORTIONAL, 4)

    def test_accepts_camel_case_and_snake_case_names(self) -> None:
        camel = parse_one({
            "matches": [{"target": {"spanAttribute": {"path": ["a"]}}, "predicate": {"gte": {"intValue": "5"}}}],
            "sampling": {"percentage": 10, "samplingPrecision": 5},
        })  # fmt: skip
        (snake,) = parse_policy_set(json.dumps({"policies": [{"id": "p", "span_sampling": {
            "matches": [{"target": {"span_attribute": {"path": ["a"]}}, "predicate": {"gte": {"int_value": 5}}}],
            "sampling": {"percentage": "10", "sampling_precision": "5"},
        }}]}))  # fmt: skip
        assert camel == snake
        assert isinstance(camel, Policy) and camel.span_sampling is not None
        (matcher,) = camel.span_sampling.matches
        assert matcher.target == SpanAttribute(("a",)) and matcher.predicate == Compare("gte", 5)

    def test_reads_every_scalar_type(self) -> None:
        def value(scalar: dict[str, Any]) -> object:
            entry = parse_one(
                {"matches": [{"target": KIND, "predicate": {"equals": scalar}}], "sampling": {"percentage": 0}}
            )
            assert isinstance(entry, Policy) and entry.span_sampling is not None, entry
            predicate = entry.span_sampling.matches[0].predicate
            assert isinstance(predicate, Equals)
            return predicate.value

        assert value({"stringValue": "x"}) == "x"
        assert value({"intValue": "-9223372036854775808"}) == -(2**63)
        assert value({"intValue": 200.0}) == 200 and isinstance(value({"intValue": 200.0}), int)
        assert value({"boolValue": False}) is False
        assert value({"doubleValue": "-Infinity"}) == float("-inf")
        assert value({"doubleValue": 3}) == 3.0 and isinstance(value({"doubleValue": 3}), float)
        assert value({"bytesValue": "eA=="}) == b"x"

    def test_reads_the_other_predicates_and_targets(self) -> None:
        entry = parse_one({
            "matches": [
                {"target": KIND, "predicate": {"contains": {"stringValue": "ERN"}}},
                {"target": {"resourceAttribute": {"path": ["service.name"]}}, "predicate": {"exists": {}}},
                {"target": {"scopeField": "SCOPE_FIELD_NAME"}, "predicate": {"lt": {"doubleValue": 1.5}}},
            ],
            "sampling": {"percentage": 100, "mode": "SAMPLING_MODE_UNSPECIFIED"},
        })  # fmt: skip
        assert isinstance(entry, Policy) and entry.span_sampling is not None
        contains, resource, scope = entry.span_sampling.matches
        assert contains.predicate == Contains("ERN")
        assert resource.target == UnsupportedTarget("resource_attribute")
        assert scope.target == UnsupportedTarget("scope_field") and scope.predicate == Compare("lt", 1.5)
        assert entry.span_sampling.sampling.mode is SamplingMode.EQUALIZING

    def test_matches_may_be_empty_or_missing(self) -> None:
        for body in ({"matches": [], "sampling": {"percentage": 5}}, {"sampling": {"percentage": 5}}):
            entry = parse_one(body)
            assert isinstance(entry, Policy) and entry.span_sampling is not None
            assert entry.span_sampling.matches == ()

    def test_an_invalid_policy_is_skipped_and_the_rest_parse(self) -> None:
        text = json.dumps({"policies": [
            {"id": "bad", "spanSampling": {"sampling": {"percentage": 150}}},
            {"id": "good", "spanSampling": {"sampling": {"percentage": 50}}},
            "not a policy",
        ]})  # fmt: skip
        bad, good, junk = parse_policy_set(text)
        assert isinstance(bad, InvalidPolicy) and bad.id == "bad" and "percentage" in bad.error
        assert isinstance(good, Policy) and good.id == "good"
        assert isinstance(junk, InvalidPolicy) and junk.id == ""

    def test_duplicate_ids_make_every_copy_invalid(self) -> None:
        policy = {"id": "twice", "spanSampling": {"sampling": {"percentage": 50}}}
        first, second, other = parse_policy_set(json.dumps({"policies": [policy, policy, {**policy, "id": "once"}]}))
        assert isinstance(first, InvalidPolicy) and isinstance(second, InvalidPolicy)
        assert "duplicate" in first.error and first.id == second.id == "twice"
        assert isinstance(other, Policy)

    def test_a_disabled_policy_is_not_validated(self) -> None:
        entry = parse_one({"sampling": {"percentage": 999}}, enabled=False)
        assert isinstance(entry, Policy) and not entry.enabled and entry.span_sampling is None

    def test_another_policy_type_is_named(self) -> None:
        (entry,) = parse_policy_set('{"policies": [{"id": "logs", "log_filter": {}}]}')
        assert isinstance(entry, Policy) and entry.span_sampling is None and entry.unsupported == "log_filter"

    def test_errors_name_their_location(self) -> None:
        assert "matches[1].target" in error_of({
            "matches": [{"target": KIND, "predicate": {"exists": {}}}, {"predicate": {"exists": {}}}],
            "sampling": {"percentage": 0},
        })  # fmt: skip
        assert "sampling.percentage" in error_of({"sampling": {"percentage": "ten"}})
        assert "matches[0].predicate.regex" in error_of({
            "matches": [{"target": KIND, "predicate": {"regex": "("}}], "sampling": {"percentage": 0},
        })  # fmt: skip
        assert "both" in error_of({"sampling": {"percentage": 0, "samplingPrecision": 4, "sampling_precision": 4}})
        assert "hashSeed" in error_of({"sampling": {"percentage": 0, "hashSeed": 1}})

    @pytest.mark.parametrize("text", ["", "{", "[]", "null", '"x"', '{"policies": {}}', '{"policies": NaN}'])
    def test_a_malformed_document_raises(self, text: str) -> None:
        with pytest.raises(PolicyDocumentError):
            parse_policy_set(text)

    @pytest.mark.parametrize("text", ["{}", '{"policies": []}', '{"policies": null}', '{"other": 1}'])
    def test_an_empty_document_is_an_empty_set(self, text: str) -> None:
        assert parse_policy_set(text) == []

    def test_documents_parse_as_the_shared_vectors_say(self) -> None:
        for name, policy_set in policy_vectors("decision")["policy_sets"].items():
            if policy_set["malformed"]:
                with pytest.raises(PolicyDocumentError):
                    parse_policy_set(policy_set["text"])
            else:
                entries = parse_policy_set(policy_set["text"])
                assert [entry.id for entry in entries] == [status["id"] for status in policy_set["statuses"]], name
