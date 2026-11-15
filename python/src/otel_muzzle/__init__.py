# SPDX-License-Identifier: Apache-2.0
"""otel-muzzle drops INTERNAL spans from OpenTelemetry traces and keeps every trace connected.

``install()`` is all most applications need. ``MuzzleSampler`` is the policy, for applications that build their own
sampler. Under ``opentelemetry-instrument``, the ``muzzle`` instrumentor entry point calls ``install()`` for you.

Usage:
    import otel_muzzle

    otel_muzzle.install()  # or otel_muzzle.install(ratio=0.05)
    # or, with span sampling policies from a file (experimental):
    otel_muzzle.install(policy_file="/etc/otel/policy.json")

    # or, with opentelemetry-instrumentation installed:
    from otel_muzzle.instrumentor import MuzzleInstrumentor

    MuzzleInstrumentor().instrument()
"""

from __future__ import annotations

import logging
import os

from opentelemetry.sdk.environment_variables import OTEL_SDK_DISABLED

from otel_muzzle import _patch
from otel_muzzle.policy import _runtime
from otel_muzzle.sampler import MuzzleSampler, _validate_policy_file, _validate_ratio
from otel_muzzle.version import __version__

__all__ = ["MuzzleSampler", "__version__", "install"]

_logger = logging.getLogger(__name__)


def install(*, ratio: float | None = None, policy_file: str | os.PathLike[str] | None = None) -> None:
    """Drops INTERNAL spans under local parents in every SDK tracer, and keeps each trace connected.

    It patches ``opentelemetry.sdk.trace.Tracer.start_span`` once, at class level, and from then on wraps each
    tracer's sampler (whatever ``OTEL_TRACES_SAMPLER`` or your code chose) in a ``MuzzleSampler`` the first time that
    tracer starts a span. So it works before or after the global tracer provider is set, and reaches tracers created
    before the call.

    With a policy file, the samplers decide by its span sampling policies instead of the built-in rule (policy mode),
    and the ratio is ignored. A file that cannot be read leaves no policy in effect: nothing is dropped.

    Calling it again changes nothing. A different explicit ``ratio`` or ``policy_file`` on a later call is logged and
    ignored. With ``OTEL_SDK_DISABLED=true`` it does nothing.

    Args:
        ratio: the share of traces that keep every span, in ``[0, 1]``. Defaults to ``OTEL_MUZZLE_VERBOSE_RATIO``,
            or 0.1 when that is unset or invalid.
        policy_file: a file of span sampling policies. Defaults to ``OTEL_MUZZLE_POLICY_FILE``. Experimental.

    Raises:
        TypeError: ``ratio`` is not a number, or ``policy_file`` is not a path.
        ValueError: ``ratio`` is outside [0, 1], or NaN.
    """
    if ratio is not None:
        ratio = _validate_ratio(ratio)
    path = _validate_policy_file(policy_file)
    if os.environ.get(OTEL_SDK_DISABLED, "").strip().lower() == "true":
        return
    active = _patch.install(ratio, path)
    policies = _patch.policies()
    if policies is not None:
        if ratio is not None:
            _logger.warning("otel-muzzle: ratio %s is ignored in policy mode", ratio)
        installed = _runtime.provider_for(policies).path
        if path is not None and os.path.realpath(path) != os.path.realpath(installed):
            _logger.warning(
                "otel-muzzle: already installed with the policy file %s, ignoring the policy file %s", installed, path
            )
    elif path is not None:
        _logger.warning("otel-muzzle: already installed without a policy file, ignoring the policy file %s", path)
    elif ratio is not None and ratio != active:
        _logger.warning(
            "otel-muzzle: already installed with ratio %s, ignoring ratio %s; under opentelemetry-instrument, "
            "set OTEL_MUZZLE_VERBOSE_RATIO instead",
            active,
            ratio,
        )
