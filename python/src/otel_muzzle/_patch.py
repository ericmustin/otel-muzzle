# SPDX-License-Identifier: Apache-2.0
"""The ``Tracer.start_span`` patch: it reparents spans a sampler drops, and holds no policy.

It also wraps each SDK tracer's sampler in a ``MuzzleSampler`` while ``install()`` is in effect.
See docs/design.md#patch.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import Any, Final

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Tracer
from opentelemetry.sdk.trace.sampling import Sampler
from opentelemetry.trace import NonRecordingSpan, Span, get_current_span

from otel_muzzle.policy import _runtime
from otel_muzzle.policy.span_sampling import SpanSamplingImplementer
from otel_muzzle.sampler import MuzzleSampler, _ratio_from_env

_MARK: Final = "_otel_muzzle_patch"

_lock = threading.Lock()
# What the samplers the patch wraps decide with while install() is in effect: the ratio of the built-in rule, or in
# policy mode the policies. Both are None when install() is not in effect.
_ratio: float | None = None
_policies: SpanSamplingImplementer | None = None


class _InstalledSampler(MuzzleSampler):
    """A ``MuzzleSampler`` that the patch put on a tracer, which it takes off again after ``uninstall()``.

    It takes its configuration as given: ``install()`` read the environment once, for every tracer.
    """

    def __init__(self, delegate: Sampler, ratio: float | None, policies: SpanSamplingImplementer | None) -> None:
        self._configure(delegate, ratio, policies)


def _sync_sampler(tracer: Tracer, ratio: float | None, policies: SpanSamplingImplementer | None = None) -> None:
    """Brings the tracer's sampler in line: wrapped, re-wrapped, or its own again when both arguments are None."""
    sampler = tracer.sampler
    if type(sampler) is _InstalledSampler:
        if ratio is None and policies is None:
            tracer.sampler = sampler.delegate
        elif sampler.ratio != ratio or sampler.policies is not policies:
            tracer.sampler = _InstalledSampler(sampler.delegate, ratio, policies)
    elif (ratio is not None or policies is not None) and not isinstance(sampler, MuzzleSampler):
        tracer.sampler = _InstalledSampler(sampler, ratio, policies)


def _patched(original: Callable[..., Span]) -> Callable[..., Span]:
    @functools.wraps(original)
    def start_span(self: Tracer, name: str, context: Context | None = None, *args: Any, **kwargs: Any) -> Span:
        _sync_sampler(self, _ratio, _policies)
        span = original(self, name, context, *args, **kwargs)
        if span.is_recording():
            return span
        # The parent the SDK used: `context`, or the current context when it is None. An explicit root
        # (context=Context()) has an invalid parent and is left alone below.
        #
        # The Python SDK ignores suppress_instrumentation when it starts a span, so there is no decision to keep.
        parent = get_current_span(context).get_span_context()
        if not parent.is_valid or parent.is_remote or not parent.trace_flags.sampled:
            return span
        # The parent's own context: trace id, span id, flags and tracestate, so nested drops chain up to the kept
        # ancestor.
        return NonRecordingSpan(parent)

    setattr(start_span, _MARK, True)
    return start_span


def is_patched() -> bool:
    return getattr(Tracer.start_span, _MARK, False) is True


def patch() -> None:
    """Applies the patch to the SDK's ``Tracer`` class, once."""
    with _lock:
        _apply()


def _apply() -> None:
    if not is_patched():
        Tracer.start_span = _patched(Tracer.start_span)  # type: ignore[method-assign]


def install(ratio: float | None, policy_file: str | None = None) -> float | None:
    """Patches and turns the lazy wrap on.

    With ``policy_file``, or ``OTEL_MUZZLE_POLICY_FILE`` when it is None, the wrap is in policy mode. Otherwise it
    uses ``ratio``, or ``OTEL_MUZZLE_VERBOSE_RATIO`` when that is None.

    Returns:
        The ratio in effect: the one an earlier call installed, which stays, or this call's. None in policy mode.
    """
    global _ratio, _policies
    with _lock:
        _apply()
        if _ratio is None and _policies is None:
            path = policy_file or _runtime.policy_file_from_env()
            if path is not None:
                _policies = _runtime.from_file(path)
            else:
                _ratio = ratio if ratio is not None else _ratio_from_env()
        return _ratio


def policies() -> SpanSamplingImplementer | None:
    """The policies the wrap decides with: None unless ``install()`` is in effect in policy mode."""
    return _policies


def uninstall() -> None:
    """Turns the lazy wrap off: each tracer gets its own sampler back the next time it starts a span.

    The patch itself stays. It holds no policy and changes nothing for a tracer without a ``MuzzleSampler``, and a
    ``MuzzleSampler`` the application set itself, or one a tracer is still sampling with on another thread, must keep
    having its drops reparented: removing the patch is what would break traces.
    """
    global _ratio, _policies
    with _lock:
        _ratio = None
        _policies = None
