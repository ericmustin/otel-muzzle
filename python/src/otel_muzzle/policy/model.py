# SPDX-License-Identifier: Apache-2.0
"""The policy model: what a policy file says, after parsing.

It mirrors the messages of ``opentelemetry.proto.policies.v1development`` that span sampling needs: ``Policy``,
``SpanSamplingPolicy``, ``SpanMatcher``, ``SpanFieldSelector``, ``Predicate`` and ``SamplingConfig``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Compare",
    "Contains",
    "Equals",
    "Exists",
    "InvalidPolicy",
    "Policy",
    "PolicyApplyStatus",
    "Predicate",
    "RecordField",
    "Regex",
    "SamplingConfig",
    "SamplingMode",
    "Scalar",
    "SpanAttribute",
    "SpanMatcher",
    "SpanRecordField",
    "SpanSamplingPolicy",
    "Target",
    "UnsupportedTarget",
]

Scalar = str | int | float | bool | bytes
"""A ``ScalarValue``. An ``int`` is an ``int_value`` and a ``float`` a ``double_value``."""


class SpanRecordField(enum.Enum):
    """``SpanRecordField``, without its ``SPAN_RECORD_FIELD_`` prefix."""

    NAME = "NAME"
    TRACE_ID = "TRACE_ID"
    SPAN_ID = "SPAN_ID"
    PARENT_SPAN_ID = "PARENT_SPAN_ID"
    TRACE_STATE = "TRACE_STATE"
    KIND = "KIND"


class SamplingMode(enum.Enum):
    """``SamplingMode``. An unspecified mode is EQUALIZING."""

    EQUALIZING = "EQUALIZING"
    PROPORTIONAL = "PROPORTIONAL"


@dataclass(frozen=True)
class RecordField:
    field: SpanRecordField


@dataclass(frozen=True)
class SpanAttribute:
    path: tuple[str, ...]


@dataclass(frozen=True)
class UnsupportedTarget:
    """A selector the specification defines and a sampler cannot read: resource and scope targets."""

    kind: str


Target = RecordField | SpanAttribute | UnsupportedTarget


@dataclass(frozen=True)
class Exists:
    pass


@dataclass(frozen=True)
class Equals:
    value: Scalar


@dataclass(frozen=True)
class Regex:
    pattern: str


@dataclass(frozen=True)
class Compare:
    """``gt``, ``gte``, ``lt`` or ``lte`` against a ``NumericValue``."""

    op: Literal["gt", "gte", "lt", "lte"]
    value: int | float


@dataclass(frozen=True)
class Contains:
    value: Scalar


Predicate = Exists | Equals | Regex | Compare | Contains


@dataclass(frozen=True)
class SpanMatcher:
    target: Target
    predicate: Predicate
    negate: bool = False


@dataclass(frozen=True)
class SamplingConfig:
    percentage: float
    mode: SamplingMode = SamplingMode.EQUALIZING
    precision: int = 4


@dataclass(frozen=True)
class SpanSamplingPolicy:
    matches: tuple[SpanMatcher, ...]
    sampling: SamplingConfig


@dataclass(frozen=True)
class Policy:
    """A policy that parsed.

    ``span_sampling`` is None for a disabled policy, whose definition is not read, and for a policy of another
    type, which ``unsupported`` then names (``log_filter``, ``log_transform`` or ``metric_filter``).
    """

    id: str
    enabled: bool = True
    span_sampling: SpanSamplingPolicy | None = None
    unsupported: str | None = None


@dataclass(frozen=True)
class InvalidPolicy:
    """A policy that did not parse. It stays inert. ``id`` is empty when the policy has no usable one."""

    id: str
    error: str


@dataclass(frozen=True)
class PolicyApplyStatus:
    """Whether a policy is in effect, and if not, why."""

    policy_id: str
    applied: bool
    error: str | None = None
