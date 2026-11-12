# SPDX-License-Identifier: Apache-2.0
"""MuzzleSampler: all of otel-muzzle's policy, as a plain OpenTelemetry sampler."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Sequence
from typing import Final

from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanContext, SpanKind, TraceState, get_current_span
from opentelemetry.util.types import Attributes

__all__ = ["MuzzleSampler"]

_RATIO_ENV: Final = "OTEL_MUZZLE_VERBOSE_RATIO"
_DEFAULT_RATIO: Final = 0.1
_TRACESTATE_KEY: Final = "muzzle"
_VERBOSE: Final = "verbose"
# What OTEL_MUZZLE_VERBOSE_RATIO accepts: a plain decimal number in ASCII digits. See docs/design.md#sampler.
_RATIO_GRAMMAR: Final = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
# Surrounding whitespace is ASCII only, so that Unicode spaces make a value invalid.
_ASCII_WHITESPACE: Final = " \t\n\v\f\r"

_logger = logging.getLogger(__name__)


class MuzzleSampler(Sampler):
    """Drops INTERNAL spans whose parent is a local span, unless the trace is verbose.

    A trace is verbose when its tracestate carries ``muzzle=verbose`` or when ``ratio`` selects its trace id. Every
    other decision is the delegate's, and the delegate's DROP always wins.

    The sampler only decides. Without the ``Tracer.start_span`` patch that ``otel_muzzle.install()`` applies, the SDK
    gives a dropped span a fresh, unsampled span id: under a ``ParentBased`` delegate (the default) its whole subtree
    is dropped, and under one that ignores the parent, such as ``ALWAYS_ON``, its children point at a parent that is
    never exported.

    Args:
        delegate: the sampler whose decisions stand. Defaults to ``ParentBased(ALWAYS_ON)``.
        ratio: the share of traces that keep every span, in ``[0, 1]``. Defaults to ``OTEL_MUZZLE_VERBOSE_RATIO``,
            or 0.1 when that is unset or invalid.
    """

    def __init__(self, delegate: Sampler | None = None, *, ratio: float | None = None) -> None:
        self._delegate: Sampler = delegate if delegate is not None else ParentBased(ALWAYS_ON)
        self._ratio = _ratio_from_env() if ratio is None else _validate_ratio(ratio)
        self._bound = math.floor(self._ratio * 2**32)

    @property
    def delegate(self) -> Sampler:
        return self._delegate

    @property
    def ratio(self) -> float:
        return self._ratio

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        # The delegate gets exactly the arguments the SDK passed, the way the SDK passes them: a sampler that
        # worked in the provider before otel-muzzle wrapped it keeps working.
        if trace_state is None:
            result = self._delegate.should_sample(parent_context, trace_id, name, kind, attributes, links)
        else:
            result = self._delegate.should_sample(
                parent_context, trace_id, name, kind, attributes, links, trace_state=trace_state
            )
        if result.decision is Decision.DROP:
            return result
        parent = get_current_span(parent_context).get_span_context()
        if self._keep(parent, trace_id, kind):
            return result
        return SamplingResult(
            Decision.DROP, None, result.trace_state if result.trace_state is not None else parent.trace_state
        )

    def get_description(self) -> str:
        return f"MuzzleSampler{{ratio={self._ratio}, delegate={self._delegate.get_description()}}}"

    def _keep(self, parent: SpanContext, trace_id: int, kind: SpanKind | None) -> bool:
        return (
            (kind is not None and kind is not SpanKind.INTERNAL)
            or not parent.is_valid
            or parent.is_remote
            or parent.trace_state.get(_TRACESTATE_KEY) == _VERBOSE
            # Trace-id bytes 12-15 (the last 8 hex characters), as a big-endian uint32, below floor(ratio * 2**32).
            or trace_id & 0xFFFFFFFF < self._bound
        )


def _validate_ratio(ratio: object) -> float:
    """Checks that ``ratio`` is a number within [0, 1].

    Args:
        ratio: the value to check.

    Returns:
        ``ratio`` as a float.

    Raises:
        TypeError: ``ratio`` is not a number (a bool is not).
        ValueError: ``ratio`` is outside [0, 1], or NaN.
    """
    message = f"otel-muzzle: ratio must be a number within [0, 1], got {ratio!r}"
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise TypeError(message)
    if not 0.0 <= ratio <= 1.0:  # NaN fails this too
        raise ValueError(message)
    return float(ratio)


def _ratio_from_env() -> float:
    """``OTEL_MUZZLE_VERBOSE_RATIO``, or 0.1 when it is unset or blank. An invalid value is logged and ignored."""
    raw = os.environ.get(_RATIO_ENV, "").strip(_ASCII_WHITESPACE)
    if not raw:
        return _DEFAULT_RATIO
    if _RATIO_GRAMMAR.fullmatch(raw) and 0.0 <= (ratio := float(raw)) <= 1.0:
        return ratio
    _logger.warning(
        "otel-muzzle: %s must be a number within [0, 1], got %s; using %s",
        _RATIO_ENV,
        json.dumps(raw, ensure_ascii=False),
        _DEFAULT_RATIO,
    )
    return _DEFAULT_RATIO
