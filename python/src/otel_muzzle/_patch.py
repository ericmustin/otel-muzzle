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
from opentelemetry.trace import NonRecordingSpan, Span, get_current_span

from otel_muzzle.sampler import MuzzleSampler, _ratio_from_env

_MARK: Final = "_otel_muzzle_patch"

_lock = threading.Lock()
# The ratio for the samplers the patch wraps while install() is in effect; None when it is not.
_ratio: float | None = None


class _InstalledSampler(MuzzleSampler):
    """A ``MuzzleSampler`` that the patch put on a tracer, which it takes off again after ``uninstall()``."""


def _sync_sampler(tracer: Tracer, ratio: float | None) -> None:
    """Brings the tracer's sampler in line with ``ratio``: wrapped, re-wrapped, or its own again when None."""
    sampler = tracer.sampler
    if type(sampler) is _InstalledSampler:
        if ratio is None:
            tracer.sampler = sampler.delegate
        elif sampler.ratio != ratio:
            tracer.sampler = _InstalledSampler(sampler.delegate, ratio=ratio)
    elif ratio is not None and not isinstance(sampler, MuzzleSampler):
        tracer.sampler = _InstalledSampler(sampler, ratio=ratio)


def _patched(original: Callable[..., Span]) -> Callable[..., Span]:
    @functools.wraps(original)
    def start_span(self: Tracer, name: str, context: Context | None = None, *args: Any, **kwargs: Any) -> Span:
        _sync_sampler(self, _ratio)
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


def install(ratio: float | None) -> float:
    """Patches and turns the lazy wrap on with ``ratio``, or ``OTEL_MUZZLE_VERBOSE_RATIO`` when it is None.

    Returns:
        The ratio in effect: the one an earlier call installed, which stays, or this call's.
    """
    global _ratio
    with _lock:
        _apply()
        if _ratio is None:
            _ratio = ratio if ratio is not None else _ratio_from_env()
        return _ratio


def uninstall() -> None:
    """Turns the lazy wrap off: each tracer gets its own sampler back the next time it starts a span.

    The patch itself stays. It holds no policy and changes nothing for a tracer without a ``MuzzleSampler``, and a
    ``MuzzleSampler`` the application set itself, or one a tracer is still sampling with on another thread, must keep
    having its drops reparented: removing the patch is what would break traces.
    """
    global _ratio
    with _lock:
        _ratio = None
