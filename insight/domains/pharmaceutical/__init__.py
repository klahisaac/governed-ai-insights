"""Pharmaceutical commercial domain pack: metrics, policy, analyzer, narrators, documents.

The core never imports this package; an app picks a domain pack and hands it to
the engine. A second domain (say, software revenue) would be a sibling package.
"""

from __future__ import annotations

from pathlib import Path

from insight.core.documents import FileDocumentStore
from insight.core.engine import Domain
from insight.core.foundry import FoundryChat
from insight.core.llm import ClaudeAPI
from insight.core.policy import Policy
from insight.core.semantic import Metric, Warehouse
from insight.domains.pharmaceutical.analyzers import LaunchAnalyzer, PersistenceAnalyzer
from insight.domains.pharmaceutical.narrators import (
    ClaudeAPINarrator,
    FoundryNarrator,
    SloppyNarrator,
    TemplateNarrator,
)

HERE = Path(__file__).parent
USERS = HERE / "users.toml"


def _regions(warehouse: Warehouse) -> list[dict[str, str]]:
    return warehouse.query("SELECT code, name FROM regions ORDER BY code", {})


def load() -> Domain:
    # Display order: the launch headline first.
    metrics = [Metric.load(HERE / "metrics" / f"{name}.toml") for name in ("new_to_brand_patients", "eight_week_persistence")]
    narrators = [TemplateNarrator()]
    if claude := ClaudeAPI.from_environment():  # only when ANTHROPIC_API_KEY is set
        narrators.append(ClaudeAPINarrator(claude))
    if foundry := FoundryChat.from_environment():  # only when a Foundry deployment is configured
        narrators.append(FoundryNarrator(foundry))
    narrators.append(SloppyNarrator())
    return Domain(
        name="pharmaceutical",
        metrics={metric.name: metric for metric in metrics},
        analyzers={"new_to_brand_patients": LaunchAnalyzer(), "eight_week_persistence": PersistenceAnalyzer()},
        narrators={narrator.name: narrator for narrator in narrators},
        default_narrator=TemplateNarrator.name,
        policy=Policy.load(HERE / "policy.toml"),
        scope_members=_regions,
        documents=FileDocumentStore(HERE / "documents"),
    )
