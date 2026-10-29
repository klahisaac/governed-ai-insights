"""Validator: every claim must be grounded in the evidence it cites and obey policy.

Owner: platform engineer. This runs on every narrative, whoever wrote the
narrator and whichever model produced it. Any violation withholds the narrative.

Evidence is facts (computed numbers) and documents (text people wrote). A number
in a claim that cites facts must come from those facts: a document's numbers ground
only a claim that cites documents alone, so a document cannot supply a figure about
the metric. Dates may come from either. The one other source is a quantity in the
governed title of the metric, as written there ("8 weeks" in "still on therapy at 8
weeks"). Movement words (rose, fell, ...) must agree with the direction of the facts
cited. Causal wording is refused whatever the evidence: a document published near a
change does not show that it explains the change.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from insight.core.claims import Claim
from insight.core.documents import Document
from insight.core.facts import Fact
from insight.core.policy import Policy
from insight.core.semantic import Metric

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
QUANTITY = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s+(\w+)")
# Comparatives (higher, lower) are left out: "naive patients were lower" can describe an
# "up" comparison of switch patients, so they say nothing reliable about direction.
MOVEMENT_WORDS = {
    "up": re.compile(r"(?<![\w-])(up|rose|risen|rise|rises|rising|increased?|increases|increasing|grew|grown|grows?|"
                     r"growing|growth|gained|gains?|climbed|jumped|surged?|surging|spiked?)(?![\w-])"),
    "down": re.compile(r"(?<![\w-])(down|fell|fallen|falls?|falling|declined?|declines|declining|decreased?|"
                       r"decreases|decreasing|dropped|drops?|dropping|slipped|plunged?|plunging|slumped?)(?![\w-])"),
}


@dataclass(frozen=True)
class Violation:
    code: str
    claim: int | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _matches(token: float, value: float) -> bool:
    return any(token in (round(candidate), round(candidate, 1)) for candidate in (value, abs(value)))


def _document_text(document: Document) -> str:
    return f"{document.title}\n{document.published_on}\n{document.body}"


def validate(
    claims: list[Claim],
    facts: list[Fact],
    policy: Policy,
    documents: list[Document] | None = None,
    metric: Metric | None = None,
) -> list[Violation]:
    facts_by_id = {fact.id: fact for fact in facts}
    # "8 weeks" in a title also allows "8-week" and "8 week", never a bare 8.
    title_quantities = [
        re.compile(rf"(?<![\w.]){re.escape(number)}[\s\-\u2010\u2011\u2013]+{re.escape(unit.lower().removesuffix('s'))}s?\b",
                   re.IGNORECASE)
        for number, unit in QUANTITY.findall(metric.title)
    ] if metric else []
    documents_by_id = {document.id: document for document in documents or []}
    violations: list[Violation] = []

    for index, claim in enumerate(claims):
        if not claim.fact_ids and not claim.document_ids:
            violations.append(Violation("no_evidence", index, "claim cites no facts or documents"))
            continue
        unknown_facts = [fact_id for fact_id in claim.fact_ids if fact_id not in facts_by_id]
        if unknown_facts:
            violations.append(Violation("unknown_fact", index, f"cites unknown facts {unknown_facts}"))
            continue
        # Includes documents that exist but were not given to this reader's summary.
        unknown_documents = [document_id for document_id in claim.document_ids if document_id not in documents_by_id]
        if unknown_documents:
            violations.append(Violation("unknown_document", index, f"cites documents not provided {unknown_documents}"))
            continue
        cited_facts = [facts_by_id[fact_id] for fact_id in claim.fact_ids]
        cited_documents = [documents_by_id[document_id] for document_id in claim.document_ids]
        if any(fact.suppressed for fact in cited_facts):
            violations.append(Violation("suppressed_fact", index, "cites a fact withheld by the small cell rule"))
            continue

        document_text = "\n".join(_document_text(document) for document in cited_documents)
        dates = {value for fact in cited_facts for value in fact.attributes.values() if isinstance(value, str)}
        dates |= set(ISO_DATE.findall(document_text))
        for date in ISO_DATE.findall(claim.text):
            if date not in dates:
                violations.append(Violation("ungrounded_date", index, f"{date} is not in the cited evidence"))

        values = [value for fact in cited_facts for value in fact.values.values()]
        if not cited_facts:
            values += [float(token) for token in NUMBER.findall(ISO_DATE.sub(" ", document_text))]
        checked = ISO_DATE.sub(" ", claim.text)
        for quantity in title_quantities:
            checked = quantity.sub(" ", checked)
        for token in NUMBER.findall(checked):
            if not any(_matches(float(token), value) for value in values):
                violations.append(Violation("ungrounded_number", index, f"{token} is not in the cited evidence"))

        text = claim.text.lower()
        for phrase in policy.forbidden_phrases:
            if phrase in text:
                violations.append(Violation("causal_language", index, f'"{phrase}" asserts a cause'))
        if any(word in text for word in policy.strong_words) and not any(
            fact.attributes.get("magnitude") == "large" for fact in cited_facts
        ):
            violations.append(Violation("overstated", index, "strong wording without a large change behind it"))

        directions = {fact.attributes["direction"] for fact in cited_facts if "direction" in fact.attributes}
        if claim.direction and directions and claim.direction not in directions:
            violations.append(
                Violation("direction_mismatch", index, f"says {claim.direction}, facts say {sorted(directions)}")
            )
        # The sentence is what readers see, so its wording is checked too, not only the direction field.
        for direction, pattern in MOVEMENT_WORDS.items():
            if directions and direction not in directions and (word := pattern.search(text)):
                violations.append(Violation(
                    "direction_mismatch", index, f'"{word.group()}" says {direction}, facts say {sorted(directions)}'
                ))

    cited_fact_ids = {fact_id for claim in claims for fact_id in claim.fact_ids}
    for fact in facts:
        if fact.required and not fact.suppressed and fact.id not in cited_fact_ids:
            violations.append(Violation("missing_required", None, f"required fact {fact.id} was not mentioned"))
    return violations
