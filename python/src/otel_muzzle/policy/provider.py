# SPDX-License-Identifier: Apache-2.0
"""The file policy provider: a policy set read from a JSON file."""

from __future__ import annotations

import logging
import os

from otel_muzzle.policy.parser import parse_policy_set
from otel_muzzle.policy.store import PolicyStore

__all__ = ["FilePolicyProvider"]

_logger = logging.getLogger(__name__)


class FilePolicyProvider:
    """Reads a policy file into a store: once at ``start()``, and again whenever ``reload()`` is called.

    It fails open. A file that cannot be read or is not a policy set leaves no policy in effect at start, and leaves
    the previous set in effect on a reload. Either way a warning names the file.
    """

    def __init__(self, path: str | os.PathLike[str], store: PolicyStore) -> None:
        self._path = os.fspath(path)
        self._store = store

    @property
    def path(self) -> str:
        return self._path

    def start(self) -> None:
        if not self._load("no policy is in effect"):
            self._store.set_policies(())

    def reload(self) -> bool:
        """Reads the file again. Returns whether its policy set is now the one in effect."""
        return self._load("keeping the previous policy set")

    def shutdown(self) -> None:
        """Nothing to release: the provider holds no thread and no open file."""

    def _load(self, otherwise: str) -> bool:
        try:
            with open(self._path, encoding="utf-8") as file:
                policies = parse_policy_set(file.read())
        except Exception as error:  # noqa: BLE001 - fail open: no error from a policy file reaches the application
            _logger.warning("otel-muzzle: cannot load the policy file %s: %s; %s", self._path, error, otherwise)
            return False
        self._store.set_policies(policies)
        return True
