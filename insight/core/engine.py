"""The insight pipeline: Source -> Semantic -> Fact -> Insight -> Delivery.

Owner: platform engineer. This is the runtime that other people's work plugs into:
metric definitions (Director), analyzers (Senior Data Scientist), narrators
(Director), policy rules (Compliance), documents (brand, market access, medical,
field). It decides nothing about their content.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from insight.core.audit import AuditLog
from insight.core.claims import Claim, Narrator
from insight.core.documents import DocumentStore, select_documents
from insight.core.facts import AnalysisContext, Fact, FactAnalyzer, WeeklySeries
from insight.core.policy import Forbidden, Policy, User
from insight.core.semantic import Metric, Warehouse, run_metric
from insight.core.validator import Violation, validate


@dataclass(frozen=True)
class Domain:
    name: str
    metrics: dict[str, Metric]
    analyzers: dict[str, FactAnalyzer]  # by metric name
    narrators: dict[str, Narrator]
    default_narrator: str
    policy: Policy
    scope_members: Callable[[Warehouse], list[dict[str, str]]]
    documents: DocumentStore
    documents_per_summary: int = 6


def with_required_caveats(claims: list[Claim], facts: list[Fact]) -> list[Claim]:
    """Required caveats are not left to the narrator: each visible required fact goes in as
    its approved statement, and a narrator's own caveat about it is dropped. A narrator claim
    that also states other facts is kept and checked like any other."""
    required = {fact.id: fact for fact in facts if fact.required and not fact.suppressed and fact.statement}
    kept = [claim for claim in claims if not (claim.fact_ids and set(claim.fact_ids) <= set(required))]
    return kept + [Claim("caveat", [fact.id], fact.statement, author="platform") for fact in required.values()]


def _weeks(first_week: str, as_of: str) -> list[str]:
    start, end = date.fromisoformat(first_week), date.fromisoformat(as_of)
    return [(start + timedelta(weeks=index)).isoformat() for index in range((end - start).days // 7 + 1)]


def _fill(rows: list[dict[str, Any]], weeks: list[str], column: str = "value") -> WeeklySeries:
    values_by_week = {row["week"]: row[column] for row in rows}
    return [(week, values_by_week.get(week, 0)) for week in weeks]


class InsightEngine:
    def __init__(self, domain: Domain, warehouse: Warehouse, audit: AuditLog | None = None):
        self.domain = domain
        self.warehouse = warehouse
        self.audit = audit
        self._narratives: dict[str, tuple[list[Claim], list[Violation], dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def build(
        self,
        metric_name: str,
        scope: str | None,
        audience: str,
        narrator_name: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """Compute series, facts and a validated narrative for one scope. No authorization here:
        `scope` must already be resolved by the policy for the reader.

        fresh=True skips the narrative cache (evaluations measure the narrator, not the cache).
        """
        domain, policy = self.domain, self.domain.policy
        metric = domain.metrics[metric_name]
        analyzer = domain.analyzers[metric_name]
        narrator = domain.narrators[narrator_name or domain.default_narrator]
        snapshot = self.warehouse.snapshot()
        weeks = _weeks(snapshot["first_week"], snapshot["as_of"])
        dimension = policy.scope_dimension
        filters = {dimension: scope} if scope else {}

        def series(rows: list[dict[str, Any]], group: str, column: str = "value") -> dict[str, WeeklySeries]:
            members = sorted({row[group] for row in rows})
            return {member: _fill([row for row in rows if row[group] == member], weeks, column) for member in members}

        # The queries are independent; on a remote warehouse each is a network round trip.
        groups = list(metric.breakdowns) + ([dimension] if scope is None and dimension not in metric.breakdowns else [])
        with ThreadPoolExecutor(max_workers=1 + len(groups)) as pool:
            total_future = pool.submit(run_metric, self.warehouse, metric, ["week"], filters)
            group_futures = {group: pool.submit(run_metric, self.warehouse, metric, ["week", group], filters)
                             for group in groups}
            total_rows = total_future.result()
            group_rows = {group: future.result() for group, future in group_futures.items()}
        weekly = _fill(total_rows, weeks)
        breakdowns = {group: series(rows, group) for group, rows in group_rows.items()}
        rate = metric.measure == "rate"
        weekly_numerator = _fill(total_rows, weeks, "numerator") if rate else []
        breakdown_numerators = {group: series(rows, group, "numerator") for group, rows in group_rows.items()} if rate else {}
        provisional = frozenset(weeks[-metric.provisional_weeks :]) if metric.provisional_weeks else frozenset()

        facts = analyzer.analyze(AnalysisContext(
            metric, scope or "ALL", weekly, provisional, breakdowns, weekly_numerator, breakdown_numerators
        ))
        policy.suppress_facts(facts)
        documents = select_documents(
            domain.documents.all(), scope, audience, snapshot["as_of"], domain.documents_per_summary
        )

        provenance = {
            "metric": metric.name,
            "metric_version": metric.version,
            "snapshot": snapshot,
            "scope": scope or "ALL",
            "audience": audience,
            "analyzer": f"{analyzer.name}@{analyzer.version}",
            "narrator": f"{narrator.name}@{narrator.version}",
            "policy_version": policy.version,
            "documents": [document.provenance() for document in documents],
        }
        insight_id = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:16]

        # Same provenance, same insight: an LLM narrator is called once per id, not per page view.
        with self._lock:
            cached = None if fresh else self._narratives.get(insight_id)
        if cached:
            claims, violations, trace = cached
            trace = {**trace, "cache": "hit"}
        else:
            claims, trace = [], {}
            try:
                narrative = narrator.narrate(metric, facts, documents, audience)
                # When it was written travels with the cached narrative, so a reader can tell
                # a stored summary from a fresh one.
                claims = with_required_caveats(narrative.claims, facts)
                trace = {**narrative.trace, "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}
                violations = validate(claims, facts, policy, documents, metric)
                with self._lock:
                    self._narratives[insight_id] = (claims, violations, trace)
            except Exception as error:  # a broken narrator must never break the numbers
                # The details can carry provider messages or parts of the prompt: they go to the
                # server log, and the reader gets the error class and a reference to find them.
                print(json.dumps({"level": "error", "event": "narrator_error", "insight_id": insight_id,
                                  "narrator": narrator.name, "error": f"{type(error).__name__}: {error}"}),
                      file=sys.stderr, flush=True)
                violations = [Violation("narrator_error", None,
                                        f"{type(error).__name__} (details in the server log, insight {insight_id})")]
        accepted = not violations

        # Where the summary may go: each claim takes the strictest level of what it cites.
        documents_by_id = {document.id: document for document in documents}

        def labelled(claim: Claim) -> dict[str, Any]:
            levels = ([policy.data_distribution] if claim.fact_ids else []) + [
                documents_by_id[document_id].distribution
                for document_id in claim.document_ids if document_id in documents_by_id
            ]
            return {**claim.to_dict(), "distribution": policy.most_restrictive(levels or [policy.data_distribution])}

        labelled_claims = [labelled(claim) for claim in claims]
        distribution = policy.most_restrictive(
            [claim["distribution"] for claim in labelled_claims] if accepted and labelled_claims
            else [policy.data_distribution]
        )

        return {
            "definition": metric.public(),
            "scope": scope or "ALL",
            "series": self._series(policy, metric, weekly, weekly_numerator, provisional, breakdowns),
            "facts": [fact.to_dict() for fact in facts],
            "documents": [document.to_dict() for document in documents],
            "insight": {
                "id": insight_id,
                "status": "accepted" if accepted else "withheld",
                "claims": labelled_claims if accepted else [],
                "distribution": distribution,
                "violations": [violation.to_dict() for violation in violations],
                # Shown to people with draft:read so they can see what the validator blocked.
                "blocked_draft": [] if accepted else labelled_claims,
                "provenance": provenance,
                "trace": trace,
            },
        }

    def _series(
        self,
        policy: Policy,
        metric: Metric,
        weekly: WeeklySeries,
        weekly_numerator: WeeklySeries,
        provisional: frozenset[str],
        breakdowns: dict[str, dict[str, WeeklySeries]],
    ) -> list[dict[str, Any]]:
        points = []
        if metric.measure == "rate":
            numerators = dict(weekly_numerator)
            for week, denominator in weekly:
                numerator = numerators[week]
                # A start week that is not followed long enough has no meaningful rate yet.
                measurable = week not in provisional and denominator > 0
                hidden = measurable and policy.hides_rate(numerator, denominator)
                shown = measurable and not hidden
                points.append({
                    "week": week,
                    "value": round(100 * numerator / denominator) if shown else None,
                    "numerator": numerator if shown else None,
                    "denominator": denominator if shown else None,
                    "suppressed": hidden,
                    "provisional": week in provisional,
                })
            return points

        parts = breakdowns.get(metric.stack_by, {}) if metric.stack_by else {}
        for week, value in weekly:
            split = {member: dict(series)[week] for member, series in parts.items()}
            # Complementary suppression: if any part is small, a visible total plus the
            # other parts would reveal it by subtraction, so the whole split is hidden.
            hidden = policy.is_small(value) or any(policy.is_small(count) for count in split.values())
            points.append({
                "week": week,
                "value": policy.suppress_count(value),
                "suppressed": policy.is_small(value),
                "provisional": week in provisional,
                "parts": {member: None if hidden else count for member, count in split.items()},
                "parts_hidden": hidden,
            })
        return points

    def view(
        self,
        metric_name: str,
        user: User,
        scope: str | None = None,
        narrator_name: str | None = None,
        regenerate: bool = False,
    ) -> dict[str, Any]:
        """Authorized, audited entry point used by the API. regenerate=True writes a new
        summary even when one is cached (an LLM writer is called again, at its cost)."""
        policy = self.domain.policy
        try:
            resolved = policy.scope(user, scope)
        except Forbidden:
            self._record(user, "view_denied", metric=metric_name, requested_scope=scope)
            raise
        result = self.build(metric_name, resolved, policy.audience(user), narrator_name, fresh=regenerate)
        insight = result["insight"]
        # A withheld draft broke the rules, so it is for the people who diagnose the writer, not readers.
        if not policy.can(user, "draft:read"):
            insight["blocked_draft"] = []
        self._record(
            user,
            "summary_regenerated" if regenerate else "view",
            metric=metric_name,
            metric_version=result["definition"]["version"],
            scope=result["scope"],
            insight_id=insight["id"],
            insight_status=insight["status"],
            distribution=insight["distribution"],
            narrator=insight["provenance"]["narrator"],
            documents=[document["id"] for document in result["documents"]],
            # Written for this request rather than served from the cache: what a model call cost and why.
            written=insight["trace"].get("cache") != "hit",
            llm_cost_usd=insight["trace"].get("cost_usd") if insight["trace"].get("cache") != "hit" else 0,
        )
        return result

    def change_minimum_cell_size(self, user: User, value: int) -> Policy:
        """Change the small cell threshold for everyone, at runtime. Administrators only,
        and audited. policy.toml is not rewritten: the approved rule stays under review,
        and a restart returns to it.
        """
        policy = self.domain.policy
        if not policy.can(user, "policy:edit"):
            self._record(user, "policy_change_denied", setting="minimum_cell_size", requested=value)
            raise Forbidden(f"{user.id} may not change the policy")
        updated = policy.with_minimum_cell_size(value)
        with self._lock:
            self.domain = replace(self.domain, policy=updated)
        self._record(
            user,
            "policy_changed",
            setting="minimum_cell_size",
            previous=policy.minimum_cell_size,
            value=value,
            policy_version=updated.version,
        )
        return updated

    def _record(self, user: User, action: str, **fields: Any) -> None:
        if self.audit:
            self.audit.record(user=user.id, role=user.role, action=action, **fields)
