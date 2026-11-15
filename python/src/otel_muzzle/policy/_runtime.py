# SPDX-License-Identifier: Apache-2.0
"""Process-wide wiring: one provider, store and implementer per policy file."""

from __future__ import annotations

import logging
import os
import threading
from typing import Final

from otel_muzzle.policy.provider import FilePolicyProvider
from otel_muzzle.policy.span_sampling import SpanSamplingImplementer
from otel_muzzle.policy.store import PolicyStore

POLICY_FILE_ENV: Final = "OTEL_MUZZLE_POLICY_FILE"
_RATIO_ENV: Final = "OTEL_MUZZLE_VERBOSE_RATIO"

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_runtimes: dict[str, tuple[SpanSamplingImplementer, FilePolicyProvider]] = {}


def policy_file_from_env() -> str | None:
    """``OTEL_MUZZLE_POLICY_FILE``, or None when it is unset or blank."""
    return os.environ.get(POLICY_FILE_ENV, "").strip() or None


def from_file(path: str) -> SpanSamplingImplementer:
    """The policies of the file at ``path``, read the first time the file is asked for."""
    key = os.path.realpath(path)
    with _lock:
        runtime = _runtimes.get(key)
        if runtime is None:
            if os.environ.get(_RATIO_ENV, "").strip():
                _logger.warning("otel-muzzle: %s is ignored, because a policy file is configured: %s", _RATIO_ENV, path)
            implementer = SpanSamplingImplementer()
            provider = FilePolicyProvider(path, PolicyStore(implementer))
            provider.start()
            runtime = _runtimes[key] = (implementer, provider)
        return runtime[0]


def provider_for(implementer: SpanSamplingImplementer) -> FilePolicyProvider:
    """The file provider that feeds ``implementer``, for ``reload()``.

    Raises:
        LookupError: ``implementer`` did not come from ``from_file``.
    """
    with _lock:
        for candidate, provider in _runtimes.values():
            if candidate is implementer:
                return provider
    raise LookupError("not the policies of a policy file")


def reload() -> bool:
    """Reads every policy file in use again. Returns whether each one loaded; a file that did not keeps its set."""
    with _lock:
        providers = [provider for _, provider in _runtimes.values()]
    return all([provider.reload() for provider in providers])


def reset() -> None:
    """Forgets every file. For tests."""
    with _lock:
        _runtimes.clear()
