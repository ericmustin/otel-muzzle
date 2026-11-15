# SPDX-License-Identifier: Apache-2.0
"""Span sampling policies, compiled and evaluated at span start. See docs/design.md#policy-mode.

Every enabled, valid ``SpanSamplingPolicy`` whose matchers all match a span contributes a rejection threshold, and
the highest one (the lowest probability) decides. A span no policy matches is kept.
"""

from __future__ import annotations

import logging
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from opentelemetry.trace import SpanContext, SpanKind, TraceState
from opentelemetry.util.types import Attributes

from otel_muzzle.policy._regex import compile_regex
from otel_muzzle.policy._threshold import parse_ot, proportional, randomness, threshold
from otel_muzzle.policy.implementer import PolicyImplementer
from otel_muzzle.policy.model import (
    Compare,
    Contains,
    Equals,
    Exists,
    InvalidPolicy,
    Policy,
    PolicyApplyStatus,
    Predicate,
    RecordField,
    Regex,
    SamplingMode,
    Scalar,
    SpanAttribute,
    SpanMatcher,
    SpanRecordField,
)

__all__ = ["SpanSamplingImplementer"]

_logger = logging.getLogger(__name__)

_ABSENT: Final = object()
_COMPARE: Final[dict[str, Callable[[Any, Any], bool]]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


class _Unsupported(Exception):
    """The policy is valid and uses something this implementation cannot evaluate."""


class _Span:
    """What a sampler knows about a span when it starts."""

    __slots__ = ("attributes", "kind", "name", "parent", "trace_id", "trace_state")

    def __init__(
        self,
        trace_id: int,
        name: str,
        kind: SpanKind | None,
        parent: SpanContext,
        trace_state: TraceState | None,
        attributes: Attributes,
    ) -> None:
        self.trace_id = trace_id
        self.name = name
        self.kind = kind
        self.parent = parent
        self.trace_state = trace_state
        self.attributes = attributes


_Matcher = Callable[[_Span], bool]

# A first-class field of a span, as the text a predicate reads. An empty text means the field is not set.
_RECORD_FIELDS: Final[dict[SpanRecordField, Callable[[_Span], str]]] = {
    SpanRecordField.NAME: lambda span: span.name,
    SpanRecordField.TRACE_ID: lambda span: f"{span.trace_id:032x}",
    SpanRecordField.PARENT_SPAN_ID: lambda span: f"{span.parent.span_id:016x}" if span.parent.is_valid else "",
    SpanRecordField.TRACE_STATE: lambda span: span.trace_state.to_header() if span.trace_state else "",
    # A span whose kind is unset reads INTERNAL, the default.
    SpanRecordField.KIND: lambda span: span.kind.name if span.kind is not None else "INTERNAL",
}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _equal(value: object, scalar: Scalar) -> bool:
    """Same type and value. An integer and a double are compared in one numeric domain, exactly."""
    if isinstance(scalar, bool):
        return isinstance(value, bool) and value is scalar
    if isinstance(scalar, (int, float)):
        return _is_number(value) and value == scalar
    return type(value) is type(scalar) and value == scalar


def _test(predicate: Predicate) -> Callable[[Any], bool]:
    """The predicate as a function of a value that is present. A value of the wrong type does not match."""
    if isinstance(predicate, Equals):
        scalar = predicate.value
        return lambda value: _equal(value, scalar)
    if isinstance(predicate, Regex):
        search: Callable[[str], re.Match[str] | None] = compile_regex(predicate.pattern).search
        return lambda value: isinstance(value, str) and search(value) is not None
    if isinstance(predicate, Compare):
        compare, number = _COMPARE[predicate.op], predicate.value
        return lambda value: _is_number(value) and compare(value, number)
    if isinstance(predicate, Contains):
        wanted = predicate.value

        def contains(value: Any) -> bool:
            if isinstance(value, (list, tuple)):
                return any(_equal(element, wanted) for element in value)
            if isinstance(value, str) and isinstance(wanted, str):
                return wanted in value
            return isinstance(value, bytes) and isinstance(wanted, bytes) and wanted in value

        return contains
    raise _Unsupported(f"predicate {type(predicate).__name__}")


def _attribute(span: _Span, path: tuple[str, ...]) -> Any:
    """The attribute at ``path``, each segment a map key, or ``_ABSENT``."""
    current: Any = span.attributes
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return _ABSENT
        current = current[segment]
    return _ABSENT if current is None else current


def _compile_matcher(matcher: SpanMatcher, where: str) -> _Matcher:
    target, predicate, negate = matcher.target, matcher.predicate, matcher.negate
    if isinstance(target, RecordField):
        read = _RECORD_FIELDS.get(target.field)
        if read is None:
            raise _Unsupported(f"{where}.target: unsupported selector SPAN_RECORD_FIELD_{target.field.name}")
        if isinstance(predicate, Exists):
            return lambda span: (read(span) != "") is not negate
        test = _test(predicate)
        return lambda span: test(read(span)) is not negate
    if isinstance(target, SpanAttribute):
        path = target.path
        if isinstance(predicate, Exists):
            return lambda span: (_attribute(span, path) is not _ABSENT) is not negate
        test = _test(predicate)

        def match(span: _Span) -> bool:
            value = _attribute(span, path)
            return (value is not _ABSENT and test(value)) is not negate

        return match
    raise _Unsupported(f"{where}.target: unsupported selector {target.kind}")


class _CompiledPolicy:
    __slots__ = ("id", "matchers", "precision", "proportional", "ratio", "threshold")

    def __init__(self, policy: Policy) -> None:
        assert policy.span_sampling is not None
        sampling = policy.span_sampling.sampling
        self.id = policy.id
        self.matchers = tuple(
            _compile_matcher(matcher, f"matches[{index}]") for index, matcher in enumerate(policy.span_sampling.matches)
        )
        self.ratio = sampling.percentage / 100
        self.precision = sampling.precision
        self.proportional = sampling.mode is SamplingMode.PROPORTIONAL
        self.threshold = threshold(self.ratio, self.precision)


class SpanSamplingImplementer(PolicyImplementer):
    """The span sampling policies in effect. ``apply_policies`` replaces them atomically; ``keep`` decides."""

    def __init__(self) -> None:
        self._compiled: tuple[_CompiledPolicy, ...] = ()
        self._failed: set[str] = set()

    @property
    def active(self) -> bool:
        """Whether any policy is in effect."""
        return bool(self._compiled)

    def apply_policies(self, policies: Sequence[Policy | InvalidPolicy]) -> list[PolicyApplyStatus]:
        statuses: list[PolicyApplyStatus] = []
        compiled: list[_CompiledPolicy] = []
        for policy in policies:
            error = None
            if isinstance(policy, InvalidPolicy):
                error = policy.error
            elif not policy.enabled:
                pass  # absent, by request
            elif policy.span_sampling is None:
                error = f"unsupported policy type: {policy.unsupported}"
            else:
                try:
                    compiled.append(_CompiledPolicy(policy))
                except (_Unsupported, ValueError) as unsupported:
                    error = str(unsupported)
            if error is not None:
                _logger.warning("otel-muzzle: policy %r is not applied: %s", policy.id, error)
            statuses.append(PolicyApplyStatus(policy.id, error is None, error))
        self._failed = set()
        self._compiled = tuple(compiled)
        return statuses

    def keep(
        self,
        trace_id: int,
        name: str,
        kind: SpanKind | None,
        parent: SpanContext,
        trace_state: TraceState | None,
        attributes: Attributes,
    ) -> bool:
        """Whether the policies keep a span.

        Args:
            trace_id: the span's trace id.
            name: the span's name at start.
            kind: the span's kind; None reads INTERNAL.
            parent: the parent's span context, invalid for a root span.
            trace_state: the span's tracestate: the sampler's, or else the parent's.
            attributes: the attributes passed at start.
        """
        compiled = self._compiled  # one read: apply_policies may swap it at any time
        if not compiled:
            return True
        span = _Span(trace_id, name, kind, parent, trace_state, attributes)
        limit = -1
        value = 0
        arriving: int | None = None
        for policy in compiled:
            try:
                if not all(matcher(span) for matcher in policy.matchers):
                    continue
            except Exception as error:  # noqa: BLE001 - fail open: a policy that cannot be evaluated matches nothing
                self._report(policy.id, error)
                continue
            if limit < 0:
                rv, arriving = parse_ot(trace_state.get("ot") if trace_state else None)
                value = randomness(trace_id, rv)
                if arriving is not None and value < arriving:
                    arriving = None  # inconsistent with the randomness
            limit = max(limit, self._threshold(policy, arriving))
        return limit < 0 or value >= limit

    @staticmethod
    def _threshold(policy: _CompiledPolicy, arriving: int | None) -> int:
        if arriving is None:
            return policy.threshold
        target = proportional(arriving, policy.ratio, policy.precision) if policy.proportional else policy.threshold
        return max(target, arriving)  # a threshold is never lowered

    def _report(self, policy_id: str, error: Exception) -> None:
        if policy_id not in self._failed:
            self._failed.add(policy_id)
            _logger.warning(
                "otel-muzzle: policy %r could not be evaluated and matches nothing until it can: %r", policy_id, error
            )
