"""Policy engine: who may see what, and which numbers may leave the system.

Owner: platform engineer builds the engine. Rule content (policy.toml) comes from
the business: the Director, Medical and Compliance.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from insight.core.facts import Fact


class Forbidden(Exception):
    pass


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str
    scope_value: str | None = None


@dataclass(frozen=True)
class Policy:
    version: str
    minimum_cell_size: int
    scope_dimension: str
    roles: dict[str, dict[str, Any]]
    forbidden_phrases: tuple[str, ...]
    strong_words: tuple[str, ...]
    # Distribution levels, least to most restrictive, and the level of computed data.
    distribution_levels: dict[str, dict[str, Any]] = field(default_factory=dict)
    data_distribution: str = "internal"
    # What policy.toml says; a runtime change is reported against these.
    approved_version: str = ""
    approved_minimum_cell_size: int = 0

    # Bounds for a runtime change of the small cell threshold.
    MINIMUM_CELL_SIZE_RANGE = (1, 50)

    @classmethod
    def load(cls, path: Path) -> Policy:
        raw = tomllib.loads(path.read_text())
        return cls(
            version=raw["version"],
            minimum_cell_size=raw["minimum_cell_size"],
            scope_dimension=raw["scope_dimension"],
            roles=raw["roles"],
            forbidden_phrases=tuple(raw["forbidden_phrases"]),
            strong_words=tuple(raw["strong_words"]),
            distribution_levels=raw["distribution"]["levels"],
            data_distribution=raw["distribution"]["data_level"],
            approved_version=raw["version"],
            approved_minimum_cell_size=raw["minimum_cell_size"],
        )

    def with_minimum_cell_size(self, value: int) -> Policy:
        """The same policy with another small cell threshold. The version names the
        change, so every insight built under it gets a different id."""
        low, high = self.MINIMUM_CELL_SIZE_RANGE
        if not low <= value <= high:
            raise ValueError(f"minimum cell size must be between {low} and {high}")
        approved = value == self.approved_minimum_cell_size
        version = self.approved_version if approved else f"{self.approved_version}+minimum-cell-size-{value}"
        return replace(self, minimum_cell_size=value, version=version)

    def public(self) -> dict[str, Any]:
        """The rule content, for readers to see what the platform enforces."""
        return {
            "version": self.version,
            "minimum_cell_size": self.minimum_cell_size,
            "scope_dimension": self.scope_dimension,
            "roles": self.roles,
            "forbidden_phrases": list(self.forbidden_phrases),
            "strong_words": list(self.strong_words),
            "distribution_levels": self.distribution_levels,
            "data_distribution": self.data_distribution,
            "approved_version": self.approved_version,
            "approved_minimum_cell_size": self.approved_minimum_cell_size,
            "minimum_cell_size_range": list(self.MINIMUM_CELL_SIZE_RANGE),
        }

    def most_restrictive(self, levels: list[str]) -> str:
        """The strictest of `levels`; an unknown level counts as the strictest there is."""
        order = list(self.distribution_levels)
        return max(levels, key=lambda level: order.index(level) if level in order else len(order))

    def audience(self, user: User) -> str:
        return self.roles[user.role]["audience"]

    def without_permissions(self, permissions: set[str]) -> Policy:
        """The same rules with `permissions` taken from every role, for example on a public
        deployment where anyone can sign in as any role. What readers see does not change, so
        the version (and every insight id) stays the same."""
        roles = {role: {**rules, "permissions": [p for p in rules.get("permissions", []) if p not in permissions]}
                 for role, rules in self.roles.items()}
        return replace(self, roles=roles)

    def can(self, user: User, permission: str) -> bool:
        return permission in self.roles[user.role].get("permissions", [])

    def views(self, user: User) -> list[str]:
        """The cards this role sees on the page."""
        return list(self.roles[user.role].get("views", []))

    def can_view(self, user: User, view: str) -> bool:
        return view in self.roles[user.role].get("views", [])

    def sees_every_member(self, user: User) -> bool:
        return self.roles[user.role]["scope"] == "all"

    def scope(self, user: User, requested: str | None) -> str | None:
        """Resolve the scope a request runs in. None means unrestricted."""
        if self.roles[user.role]["scope"] == "all":
            return requested
        if requested not in (None, user.scope_value):
            raise Forbidden(f"{user.id} may only view {self.scope_dimension}={user.scope_value}")
        return user.scope_value

    def is_small(self, count: float) -> bool:
        return 0 < count < self.minimum_cell_size

    def suppress_count(self, count: int) -> int | None:
        return None if self.is_small(count) else count

    def hides_rate(self, numerator: int, denominator: int) -> bool:
        """A rate reveals its numerator, its denominator and their difference."""
        return any(self.is_small(count) for count in (numerator, denominator, denominator - numerator))

    def suppress_facts(self, facts: list[Fact]) -> None:
        for fact in facts:
            if any(self.is_small(fact.values.get(field, 0)) for field in fact.count_fields):
                fact.suppressed = True
                fact.values = {}
                fact.attributes = {"withheld_by": "small cell policy", "policy_version": self.version}
