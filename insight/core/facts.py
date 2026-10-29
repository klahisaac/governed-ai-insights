"""Facts: deterministic, typed results computed from governed metrics.

Owner: platform engineer defines the Fact contract. Analyzers that produce facts
live in domain packs and belong to the analytics owner; at Celldex, the Senior
Data Scientist.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from insight.core.semantic import Metric

WeeklySeries = list[tuple[str, int]]


@dataclass
class Fact:
    id: str
    kind: str  # level | change | trend | mix | comparison | caveat
    metric: str
    metric_version: str
    scope: str
    values: dict[str, float]
    attributes: dict[str, Any] = field(default_factory=dict)
    # Values that are patient counts, so the small cell rule applies to them.
    count_fields: tuple[str, ...] = ()
    # A required fact must be in every summary (for example, a data caveat). Its approved
    # wording is `statement`, written by the analyzer; the platform adds it, not the narrator.
    required: bool = False
    statement: str | None = None
    suppressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisContext:
    metric: Metric
    scope: str
    weekly: WeeklySeries
    provisional_weeks: frozenset[str]
    # dimension -> member -> weekly series, for example {"source_of_business": {"switch": [...]}}
    breakdowns: dict[str, dict[str, WeeklySeries]]
    # For a rate metric, the numerator counts behind `weekly` and `breakdowns`.
    weekly_numerator: WeeklySeries = field(default_factory=list)
    breakdown_numerators: dict[str, dict[str, WeeklySeries]] = field(default_factory=dict)


class FactAnalyzer(Protocol):
    name: str
    version: str

    def analyze(self, context: AnalysisContext) -> list[Fact]: ...
