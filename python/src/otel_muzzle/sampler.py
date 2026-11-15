# SPDX-License-Identifier: Apache-2.0
"""MuzzleSampler: all of otel-muzzle's policy, as a plain OpenTelemetry sampler.

The policy is the built-in rule, or in policy mode a set of span sampling policies read from a file. See
docs/design.md#sampler and docs/design.md#policy-mode.
"""

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

from otel_muzzle.policy import _runtime
from otel_muzzle.policy._threshold import erase_th
from otel_muzzle.policy.span_sampling import SpanSamplingImplementer

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

    In policy mode the rule above is replaced by span sampling policies: a span the delegate records is dropped when
    the policies that match it say so, and kept otherwise. Policy mode is on when ``policies`` or ``policy_file`` is
    given, or when ``OTEL_MUZZLE_POLICY_FILE`` names a file. ``ratio`` is then ignored.

    Args:
        delegate: the sampler whose decisions stand. Defaults to ``ParentBased(ALWAYS_ON)``.
        ratio: the share of traces that keep every span, in ``[0, 1]``. Defaults to ``OTEL_MUZZLE_VERBOSE_RATIO``,
            or 0.1 when that is unset or invalid.
        policies: the span sampling policies to decide with. Experimental.
        policy_file: a policy file to read them from. Defaults to ``OTEL_MUZZLE_POLICY_FILE``. Experimental.

    Raises:
        TypeError: ``ratio`` is not a number, or ``policy_file`` is not a path.
        ValueError: ``ratio`` is outside [0, 1], or NaN.
    """

    def __init__(
        self,
        delegate: Sampler | None = None,
        *,
        ratio: float | None = None,
        policies: SpanSamplingImplementer | None = None,
        policy_file: str | os.PathLike[str] | None = None,
    ) -> None:
        if ratio is not None:
            ratio = _validate_ratio(ratio)
        if policies is None:
            path = _validate_policy_file(policy_file) or _runtime.policy_file_from_env()
            if path is not None:
                policies = _runtime.from_file(path)
        if policies is None:
            ratio = _ratio_from_env() if ratio is None else ratio
        elif ratio is not None:
            _logger.warning("otel-muzzle: ratio %s is ignored in policy mode", ratio)
            ratio = None
        self._configure(delegate if delegate is not None else ParentBased(ALWAYS_ON), ratio, policies)

    def _configure(self, delegate: Sampler, ratio: float | None, policies: SpanSamplingImplementer | None) -> None:
        self._delegate = delegate
        self._ratio = ratio
        self._bound = math.floor((ratio or 0.0) * 2**32)
        self._policies = policies

    @property
    def delegate(self) -> Sampler:
        return self._delegate

    @property
    def ratio(self) -> float | None:
        """The verbose ratio of the built-in rule. None in policy mode."""
        return self._ratio

    @property
    def policies(self) -> SpanSamplingImplementer | None:
        """The span sampling policies that decide in policy mode. None otherwise."""
        return self._policies

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
        # The tracestate the span would get: the delegate's, or else the parent's.
        state = result.trace_state if result.trace_state is not None else parent.trace_state
        if self._policies is not None:
            if self._policies.keep(trace_id, name, kind, parent, state, attributes):
                return result
            # A dropped span carries no sampling threshold, so that nothing downstream samples by one.
            return SamplingResult(Decision.DROP, None, _without_th(state))
        if self._keep(parent, trace_id, kind):
            return result
        return SamplingResult(Decision.DROP, None, state)

    def get_description(self) -> str:
        mode = "mode=policy" if self._policies is not None else f"ratio={self._ratio}"
        return f"MuzzleSampler{{{mode}, delegate={self._delegate.get_description()}}}"

    def _keep(self, parent: SpanContext, trace_id: int, kind: SpanKind | None) -> bool:
        return (
            (kind is not None and kind is not SpanKind.INTERNAL)
            or not parent.is_valid
            or parent.is_remote
            or parent.trace_state.get(_TRACESTATE_KEY) == _VERBOSE
            # Trace-id bytes 12-15 (the last 8 hex characters), as a big-endian uint32, below floor(ratio * 2**32).
            or trace_id & 0xFFFFFFFF < self._bound
        )


def _without_th(state: TraceState | None) -> TraceState | None:
    """``state`` without the ``th`` sub-key of its ``ot`` member; ``state`` itself when it has none."""
    ot = state.get("ot") if state else None
    if ot is None or state is None:
        return state
    erased = erase_th(ot)
    if erased == ot:
        return state
    return state.delete("ot") if erased is None else state.update("ot", erased)


def _validate_policy_file(policy_file: object) -> str | None:
    """Checks that ``policy_file`` is a path, and returns it as a string. None stays None.

    Raises:
        TypeError: ``policy_file`` is neither a string nor an ``os.PathLike`` of one.
    """
    if policy_file is None:
        return None
    if isinstance(policy_file, (str, os.PathLike)) and isinstance(path := os.fspath(policy_file), str):
        return path
    raise TypeError(f"otel-muzzle: policy_file must be a path, got {policy_file!r}")


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
