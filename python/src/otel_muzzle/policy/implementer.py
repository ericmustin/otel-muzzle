# SPDX-License-Identifier: Apache-2.0
"""What a policy store hands a policy set to."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from otel_muzzle.policy.model import InvalidPolicy, Policy, PolicyApplyStatus

__all__ = ["PolicyImplementer"]


class PolicyImplementer(ABC):
    """Puts a policy set into effect in the running SDK."""

    @abstractmethod
    def apply_policies(self, policies: Sequence[Policy | InvalidPolicy]) -> Sequence[PolicyApplyStatus]:
        """Replaces the set in effect with ``policies``, and returns one status per policy, in order."""
