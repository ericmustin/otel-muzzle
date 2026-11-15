# SPDX-License-Identifier: Apache-2.0
"""Reads a ``PolicySet`` from its Protobuf JSON form.

Keys may be written in lowerCamelCase or in the proto's snake_case, enum values are names, and 64-bit integers and
floating point numbers may be strings, as Protobuf JSON allows. A policy that is not valid is returned as an
``InvalidPolicy`` and never stops the others from parsing. Inside a span sampling policy an unknown key is an error,
because ignoring one could widen what the policy matches.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections import Counter
from typing import Any, Final, NoReturn

from otel_muzzle.policy._regex import compile_regex
from otel_muzzle.policy.model import (
    Compare,
    Contains,
    Equals,
    Exists,
    InvalidPolicy,
    Policy,
    Predicate,
    RecordField,
    Regex,
    SamplingConfig,
    SamplingMode,
    Scalar,
    SpanAttribute,
    SpanMatcher,
    SpanRecordField,
    SpanSamplingPolicy,
    Target,
    UnsupportedTarget,
)

__all__ = ["PolicyDocumentError", "parse_policy_set"]

_MISSING: Final = object()
_INT64_MIN: Final = -(2**63)
_INT64_MAX: Final = 2**63 - 1
_MAX_SAFE_INTEGER: Final = 2**53 - 1
_INTEGER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER: Final = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NON_FINITE: Final = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}
_BASE64: Final = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_RECORD_FIELD_PREFIX: Final = "SPAN_RECORD_FIELD_"
_MODES: Final = {
    "SAMPLING_MODE_UNSPECIFIED": SamplingMode.EQUALIZING,
    "SAMPLING_MODE_EQUALIZING": SamplingMode.EQUALIZING,
    "SAMPLING_MODE_PROPORTIONAL": SamplingMode.PROPORTIONAL,
}
# Each definition as (lowerCamelCase, snake_case).
_SPAN_SAMPLING: Final = ("spanSampling", "span_sampling")
_OTHER_DEFINITIONS: Final = (
    ("logFilter", "log_filter"),
    ("logTransform", "log_transform"),
    ("metricFilter", "metric_filter"),
)
_TARGETS: Final = (
    ("recordField", "record_field"),
    ("spanAttribute", "span_attribute"),
    ("resourceAttribute", "resource_attribute"),
    ("scopeAttribute", "scope_attribute"),
    ("scopeField", "scope_field"),
)
_SCALARS: Final = (
    ("stringValue", "string_value"),
    ("intValue", "int_value"),
    ("boolValue", "bool_value"),
    ("doubleValue", "double_value"),
    ("bytesValue", "bytes_value"),
)
_NUMERICS: Final = (("intValue", "int_value"), ("doubleValue", "double_value"))
_PREDICATES: Final = ("exists", "equals", "regex", "gt", "gte", "lt", "lte", "contains")


class PolicyDocumentError(ValueError):
    """The text holds no policy set at all: it is not JSON, or not the JSON of a ``PolicySet``."""


class _Invalid(Exception):
    """One policy is invalid. The message names where."""


def _fail(where: str, reason: str) -> NoReturn:
    raise _Invalid(f"{where}: {reason}")


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


def parse_policy_set(text: str) -> list[Policy | InvalidPolicy]:
    """Parses a policy file. The result has one entry per policy, in document order.

    Raises:
        PolicyDocumentError: the text is not the JSON of a policy set.
    """
    try:
        document = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise PolicyDocumentError(f"not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise PolicyDocumentError("a policy set must be a JSON object")
    policies = document.get("policies")
    if policies is None:
        return []
    if not isinstance(policies, list):
        raise PolicyDocumentError("policies must be a list")
    entries = [_policy(policy, index) for index, policy in enumerate(policies)]
    # An id names one policy. A repeated id makes every copy invalid: keeping the first or the last would make the
    # outcome depend on the order of the set, which the specification rules out. A disabled policy is absent.
    counts = Counter(
        entry.id for entry in entries if entry.id and not (isinstance(entry, Policy) and not entry.enabled)
    )
    return [
        InvalidPolicy(entry.id, f"policies[{index}].id: duplicate id {entry.id!r}")
        if counts[entry.id] > 1 and not (isinstance(entry, Policy) and not entry.enabled)
        else entry
        for index, entry in enumerate(entries)
    ]


def _policy(raw: Any, index: int) -> Policy | InvalidPolicy:
    where = f"policies[{index}]"
    policy_id = raw.get("id") if isinstance(raw, dict) else None
    policy_id = policy_id if isinstance(policy_id, str) else ""
    try:
        if not isinstance(raw, dict):
            _fail(where, "a policy must be an object")
        if not policy_id:
            _fail(f"{where}.id", "a policy needs a non-empty string id")
        enabled = raw.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            _fail(f"{where}.enabled", "must be a boolean")
        if enabled is False:
            return Policy(policy_id, enabled=False)
        return _definition(raw, policy_id, where)
    except _Invalid as error:
        return InvalidPolicy(policy_id, str(error))


def _definition(raw: dict[str, Any], policy_id: str, where: str) -> Policy:
    span_sampling = _get(raw, _SPAN_SAMPLING, where)
    others = [snake for camel, snake in _OTHER_DEFINITIONS if _get(raw, (camel, snake), where) is not _MISSING]
    count = len(others) + (span_sampling is not _MISSING)
    if count == 0:
        _fail(where, "a policy needs a definition")
    if count > 1:
        _fail(where, "a policy has exactly one definition")
    if others:
        return Policy(policy_id, unsupported=others[0])
    return Policy(policy_id, span_sampling=_span_sampling(span_sampling, f"{where}.spanSampling"))


def _get(obj: dict[str, Any], names: tuple[str, str], where: str) -> Any:
    """The value under either spelling of a key, or ``_MISSING``. A null is missing, as in Protobuf JSON."""
    camel, snake = names
    if camel != snake and camel in obj and snake in obj:
        _fail(where, f"both {camel} and {snake} are set")
    value = obj.get(camel, obj.get(snake))
    return _MISSING if value is None else value


def _object(value: Any, where: str, *known: tuple[str, str]) -> dict[str, Any]:
    """Checks that ``value`` is an object with no key outside ``known``."""
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    allowed = {name for names in known for name in names}
    for key in value:
        if key not in allowed:
            _fail(where, f"unknown key {key!r}")
    return value


def _one_of(obj: dict[str, Any], choices: tuple[tuple[str, str], ...], where: str) -> tuple[str, Any]:
    """The one variant that is set, as (snake_case name, value)."""
    found = [(snake, value) for camel, snake in choices if (value := _get(obj, (camel, snake), where)) is not _MISSING]
    if len(found) != 1:
        _fail(where, "exactly one of " + ", ".join(camel for camel, _ in choices) + " must be set")
    return found[0]


def _span_sampling(raw: Any, where: str) -> SpanSamplingPolicy:
    obj = _object(raw, where, ("matches", "matches"), ("sampling", "sampling"))
    matches = _get(obj, ("matches", "matches"), where)
    if matches is _MISSING:
        matches = []
    if not isinstance(matches, list):
        _fail(f"{where}.matches", "must be a list")
    matchers = tuple(_matcher(matcher, f"{where}.matches[{index}]") for index, matcher in enumerate(matches))
    sampling = _get(obj, ("sampling", "sampling"), where)
    if sampling is _MISSING:
        _fail(f"{where}.sampling", "must be set")
    return SpanSamplingPolicy(matchers, _sampling(sampling, f"{where}.sampling"))


def _sampling(raw: Any, where: str) -> SamplingConfig:
    precision_names = ("samplingPrecision", "sampling_precision")
    obj = _object(raw, where, ("percentage", "percentage"), ("mode", "mode"), precision_names)
    percentage = _get(obj, ("percentage", "percentage"), where)
    if percentage is _MISSING:
        _fail(f"{where}.percentage", "must be set")
    value = _double(percentage, f"{where}.percentage")
    if not 0.0 <= value <= 100.0:  # NaN fails this too
        _fail(f"{where}.percentage", "must be within 0 to 100")
    mode = _get(obj, ("mode", "mode"), where)
    if mode is _MISSING:
        mode = "SAMPLING_MODE_UNSPECIFIED"
    if not isinstance(mode, str) or mode not in _MODES:
        _fail(f"{where}.mode", f"unknown sampling mode {mode!r}")
    precision = _get(obj, precision_names, where)
    digits = 4 if precision is _MISSING else _integer(precision, f"{where}.samplingPrecision")
    if not 1 <= digits <= 14:
        _fail(f"{where}.samplingPrecision", "must be within 1 to 14")
    return SamplingConfig(value, _MODES[mode], digits)


def _matcher(raw: Any, where: str) -> SpanMatcher:
    obj = _object(raw, where, ("target", "target"), ("predicate", "predicate"), ("negate", "negate"))
    target = _get(obj, ("target", "target"), where)
    if target is _MISSING:
        _fail(f"{where}.target", "must be set")
    predicate = _get(obj, ("predicate", "predicate"), where)
    if predicate is _MISSING:
        _fail(f"{where}.predicate", "must be set")
    negate = _get(obj, ("negate", "negate"), where)
    if negate is not _MISSING and not isinstance(negate, bool):
        _fail(f"{where}.negate", "must be a boolean")
    return SpanMatcher(_target(target, f"{where}.target"), _predicate(predicate, f"{where}.predicate"), negate is True)


def _target(raw: Any, where: str) -> Target:
    kind, value = _one_of(_object(raw, where, *_TARGETS), _TARGETS, where)
    if kind == "record_field":
        if not isinstance(value, str) or not value.startswith(_RECORD_FIELD_PREFIX):
            _fail(f"{where}.recordField", f"unknown span record field {value!r}")
        try:
            return RecordField(SpanRecordField(value.removeprefix(_RECORD_FIELD_PREFIX)))
        except ValueError:
            _fail(f"{where}.recordField", f"unknown span record field {value!r}")
    if kind == "scope_field":
        if not isinstance(value, str):
            _fail(f"{where}.scopeField", "must be a scope field name")
        return UnsupportedTarget(kind)
    path = _get(_object(value, f"{where}.{kind}", ("path", "path")), ("path", "path"), where)
    if not isinstance(path, list) or not path or not all(isinstance(segment, str) for segment in path):
        _fail(f"{where}.{kind}.path", "must be a non-empty list of strings")
    return SpanAttribute(tuple(path)) if kind == "span_attribute" else UnsupportedTarget(kind)


def _predicate(raw: Any, where: str) -> Predicate:
    choices = tuple((name, name) for name in _PREDICATES)
    kind, value = _one_of(_object(raw, where, *choices), choices, where)
    at = f"{where}.{kind}"
    if kind == "exists":
        _object(value, at)
        return Exists()
    if kind == "equals":
        return Equals(_scalar(value, at))
    if kind == "contains":
        return Contains(_scalar(value, at))
    if kind == "regex":
        if not isinstance(value, str):
            _fail(at, "must be a string")
        try:
            compile_regex(value)
        except ValueError as error:
            _fail(at, str(error))
        return Regex(value)
    name, number = _one_of(_object(value, at, *_NUMERICS), _NUMERICS, at)
    compared = _integer(number, f"{at}.intValue") if name == "int_value" else _double(number, f"{at}.doubleValue")
    return Compare(kind, compared)  # type: ignore[arg-type]


def _scalar(raw: Any, where: str) -> Scalar:
    name, value = _one_of(_object(raw, where, *_SCALARS), _SCALARS, where)
    if name == "string_value":
        if not isinstance(value, str):
            _fail(f"{where}.stringValue", "must be a string")
        return value
    if name == "bool_value":
        if not isinstance(value, bool):
            _fail(f"{where}.boolValue", "must be a boolean")
        return value
    if name == "int_value":
        return _integer(value, f"{where}.intValue")
    if name == "double_value":
        return _double(value, f"{where}.doubleValue")
    if not isinstance(value, str) or not _BASE64.fullmatch(value):
        _fail(f"{where}.bytesValue", "must be base64")
    return base64.b64decode(value)


def _integer(value: Any, where: str) -> int:
    """A 64-bit integer: a whole JSON number that every JSON reader holds exactly, or decimal text."""
    if isinstance(value, str):
        # A 64-bit integer has 20 characters at most, and int() refuses a very long text.
        if len(value) <= 20 and _INTEGER.fullmatch(value) and _INT64_MIN <= (number := int(value)) <= _INT64_MAX:
            return number
    elif isinstance(value, int) and not isinstance(value, bool):
        # Compared as an integer: one too large for a float cannot be converted to it.
        if abs(value) <= _MAX_SAFE_INTEGER:
            return value
    elif isinstance(value, float):
        if math.isfinite(value) and value == math.floor(value) and abs(value) <= _MAX_SAFE_INTEGER:
            return int(value)
    _fail(where, "must be a 64-bit integer")


def _double(value: Any, where: str) -> float:
    if isinstance(value, str):
        if value in _NON_FINITE:
            return _NON_FINITE[value]
        if _NUMBER.fullmatch(value):
            return float(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value)
        except OverflowError:
            # A whole number beyond the range of a double is an infinity, as it is to other JSON readers.
            return math.inf if value > 0 else -math.inf
    _fail(where, "must be a number")
