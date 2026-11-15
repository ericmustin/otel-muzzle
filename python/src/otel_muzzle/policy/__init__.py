# SPDX-License-Identifier: Apache-2.0
"""Policy mode: span sampling policies from the OpenTelemetry Telemetry Policy specification.

Experimental. The specification is a draft; this package follows ``opentelemetry.proto.policies.v1development`` as
of opentelemetry-proto commit 04be8b6cf9649852ce5028dacbc9fb6cc00bd8a2. See docs/policy-conformance.md for what is
and is not supported.
"""

from __future__ import annotations

from otel_muzzle.policy._runtime import reload

__all__ = ["reload"]
