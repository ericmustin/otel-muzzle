# SPDX-License-Identifier: Apache-2.0
"""The ``muzzle`` entry point that ``opentelemetry-instrument`` loads.

This is the only module that imports ``opentelemetry-instrumentation``, which otel-muzzle does not depend on: it is
not needed at runtime, and ``opentelemetry-instrument``, which loads this module, ships it. ``import otel_muzzle``
works without it.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from otel_muzzle import _patch, install
from otel_muzzle.package import _instruments

__all__ = ["MuzzleInstrumentor"]


class MuzzleInstrumentor(BaseInstrumentor):
    """Calls ``otel_muzzle.install()``. ``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=muzzle`` turns it off.

    Under ``opentelemetry-instrument`` the ratio comes from ``OTEL_MUZZLE_VERBOSE_RATIO``.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        install(ratio=kwargs.get("ratio"))

    def _uninstrument(self, **kwargs: Any) -> None:
        # Stops wrapping: every tracer gets its own sampler back the next time it starts a span. The start_span patch
        # stays, so that no span otel-muzzle drops is ever left without its children reparented.
        _patch.uninstall()
