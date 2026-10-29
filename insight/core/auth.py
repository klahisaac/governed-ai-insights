"""Identity. Owner: platform engineer.

Production replaces PrototypeIdentityProvider with Entra ID (on Azure) or Keycloak
(on-premises): validate the OIDC token, map app roles to policy role names.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from insight.core.policy import User


class IdentityProvider(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> User | None: ...


class PrototypeIdentityProvider:
    """Bearer tokens from a file, for the prototype and for tests. Never for real users."""

    def __init__(self, path: Path):
        entries = tomllib.loads(path.read_text())["users"]
        self._users_by_token = {
            entry["token"]: User(entry["id"], entry["name"], entry["role"], entry.get("scope_value"))
            for entry in entries
        }

    def authenticate(self, headers: Mapping[str, str]) -> User | None:
        scheme, _, token = (headers.get("Authorization") or "").partition(" ")
        return self._users_by_token.get(token) if scheme == "Bearer" else None

    def prototype_logins(self) -> list[dict[str, str]]:
        return [{"token": token, "name": user.name, "role": user.role} for token, user in self._users_by_token.items()]
