# SPDX-License-Identifier: Apache-2.0
"""The policy store: the one place a provider delivers a policy set to."""

from __future__ import annotations

from collections.abc import Sequence

from otel_muzzle.policy.implementer import PolicyImplementer
from otel_muzzle.policy.model import InvalidPolicy, Policy, PolicyApplyStatus

__all__ = ["PolicyStore"]


class PolicyStore:
    """Hands each new policy set to the implementer, and keeps the status of every policy in it.

    A consumer replaces its whole set when it receives a new one: nothing of the previous set stays in effect.
    """

    def __init__(self, implementer: PolicyImplementer) -> None:
        self._implementer = implementer
        self._statuses: tuple[PolicyApplyStatus, ...] = ()

    @property
    def statuses(self) -> tuple[PolicyApplyStatus, ...]:
        """One status per policy of the set in effect, in document order."""
        return self._statuses

    def set_policies(self, policies: Sequence[Policy | InvalidPolicy]) -> tuple[PolicyApplyStatus, ...]:
        self._statuses = tuple(self._implementer.apply_policies(policies))
        return self._statuses
