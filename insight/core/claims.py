"""Claims: what a narrator says, each sentence tied to the evidence behind it.

Owner: platform engineer defines the contract. Narrators that implement it live
in domain packs and belong to the AI layer owner; at Celldex, the Director of
Commercial Data Science, writing Python.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from insight.core.documents import Document
from insight.core.facts import Fact
from insight.core.semantic import Metric


@dataclass
class Claim:
    # The cited fact kinds (change, trend, mix, comparison, caveat, level), or
    # context for a claim that draws on documents.
    type: str
    fact_ids: list[str]
    text: str
    direction: str | None = None  # up | down | flat, checked against the cited facts
    document_ids: list[str] = field(default_factory=list)
    # "narrator", or "platform" for the approved caveats the engine adds to every summary.
    author: str = "narrator"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Narrative:
    claims: list[Claim]
    # How it was produced (model, tokens, cost, latency). Empty for deterministic narrators.
    trace: dict[str, Any] = field(default_factory=dict)


class Narrator(Protocol):
    name: str
    version: str
    title: str  # what readers see; `name` and `version` are what provenance records

    def narrate(self, metric: Metric, facts: list[Fact], documents: list[Document], audience: str) -> Narrative: ...
