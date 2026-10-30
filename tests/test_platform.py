"""Invariants the platform guarantees, whatever plugs into it.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from insight.core.audit import AuditLog
from insight.core.claims import Claim
from insight.core.documents import Document
from insight.core.engine import InsightEngine, with_required_caveats
from insight.core.facts import AnalysisContext, Fact
from insight.core.llm import LLMError, LLMResult
from insight.core.policy import Forbidden, User
from insight.core.semantic import CachedWarehouse, SqliteWarehouse, run_metric
from insight.core.validator import validate
from insight.domains import pharmaceutical
from insight.domains.pharmaceutical import synthetic_warehouse
from insight.domains.pharmaceutical.analyzers import LaunchAnalyzer
from insight.domains.pharmaceutical.narrators import LLMNarrator

METRIC = "new_to_brand_patients"
PERSISTENCE = "eight_week_persistence"
SOUTHEAST_DOCUMENT = "market-access-southeast-prior-authorization"


class FakeLLM:
    def __init__(self, fail: bool = False):
        self.fail, self.prompts = fail, []

    def complete(self, system, prompt, schema):
        self.prompts.append(prompt)
        if self.fail:
            raise LLMError("not logged in")
        return LLMResult({"claims": []}, "fake", {}, 1, 1, 0.0, 1)


class ScriptedLLM:
    """Returns the claims it was given, as if a model had written them."""

    def __init__(self, claims: list[dict]):
        self.claims = claims

    def complete(self, system, prompt, schema):
        return LLMResult({"claims": self.claims}, "scripted", {}, 1, 1, 0.0, 1)


class StaticDocuments:
    def __init__(self, documents: list[Document]):
        self.documents = documents

    def all(self) -> list[Document]:
        return self.documents


class PlatformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(cls.temporary_directory.name) / "warehouse.db"
        synthetic_warehouse.build(database, seed=7)
        cls.domain = pharmaceutical.load()
        cls.engine = InsightEngine(cls.domain, SqliteWarehouse(database))
        cls.metric = cls.domain.metrics[METRIC]
        cls.policy = cls.domain.policy

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def _engine_with_llm(self, llm: FakeLLM) -> InsightEngine:
        domain = replace(self.domain, narrators={"llm": LLMNarrator(llm)})
        return InsightEngine(domain, self.engine.warehouse)

    # Semantic layer

    def test_regions_sum_to_national(self) -> None:
        national = {row["week"]: row["value"] for row in run_metric(self.engine.warehouse, self.metric, ["week"])}
        summed: dict[str, int] = {}
        for row in run_metric(self.engine.warehouse, self.metric, ["week", "region"]):
            summed[row["week"]] = summed.get(row["week"], 0) + row["value"]
        self.assertEqual(national, summed)

    def test_sources_sum_to_total(self) -> None:
        national = {row["week"]: row["value"] for row in run_metric(self.engine.warehouse, self.metric, ["week"])}
        summed: dict[str, int] = {}
        for row in run_metric(self.engine.warehouse, self.metric, ["week", "source_of_business"]):
            summed[row["week"]] = summed.get(row["week"], 0) + row["value"]
        self.assertEqual(national, summed)

    def test_unknown_dimension_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_metric(self.engine.warehouse, self.metric, ["week", "patient_id"])

    def test_cache_serves_one_snapshot_and_drops_it_when_a_new_one_lands(self) -> None:
        class Counting:
            dialect = "sqlite"

            def __init__(self, inner):
                self.inner, self.queries, self.source = inner, 0, "a"

            def query(self, sql, parameters):
                self.queries += 1
                return self.inner.query(sql, parameters)

            def snapshot(self):
                return {**self.inner.snapshot(), "source_hash": self.source}

        inner = Counting(self.engine.warehouse)
        cached = CachedWarehouse(inner, snapshot_time_to_live_seconds=0)
        first = run_metric(cached, self.metric, ["week"])
        first[0]["value"] = -1  # callers cannot corrupt the cache
        self.assertEqual(run_metric(cached, self.metric, ["week"]), run_metric(self.engine.warehouse, self.metric, ["week"]))
        self.assertEqual(inner.queries, 1)
        inner.source = "b"
        run_metric(cached, self.metric, ["week"])
        self.assertEqual(inner.queries, 2)

    def test_a_background_refresh_never_makes_a_reader_wait(self) -> None:
        class Slow:
            dialect = "sqlite"

            def __init__(self, inner):
                self.inner, self.source, self.release = inner, "a", threading.Event()
                self.release.set()

            def query(self, sql, parameters):
                return self.inner.query(sql, parameters)

            def snapshot(self):
                self.release.wait(10)  # an idle warehouse starting up
                return {**self.inner.snapshot(), "source_hash": self.source}

        inner = Slow(self.engine.warehouse)
        cached = CachedWarehouse(inner, snapshot_time_to_live_seconds=0, refresh_in_background=True)
        self.assertEqual(cached.snapshot()["source_hash"], "a")  # the first read is inline
        inner.release.clear()
        inner.source = "b"
        started = time.monotonic()
        self.assertEqual(cached.snapshot()["source_hash"], "a")  # served while the check waits
        self.assertLess(time.monotonic() - started, 1)
        inner.release.set()
        for _ in range(100):
            if cached.snapshot()["source_hash"] == "b":
                break
            time.sleep(0.02)
        self.assertEqual(cached.snapshot()["source_hash"], "b")

    # Policy

    def test_regional_sales_director_cannot_see_another_region(self) -> None:
        director = User("user", "director", "regional_sales_director", "NE")
        with self.assertRaises(Forbidden):
            self.engine.view(METRIC, director, "SE")
        self.assertEqual(self.engine.view(METRIC, director)["scope"], "NE")

    def test_small_cells_never_leave_the_engine(self) -> None:
        small = lambda value: value is not None and 0 < value < self.policy.minimum_cell_size  # noqa: E731
        for metric in (METRIC, PERSISTENCE):
            for scope in ("NE", "SE", "MW", "W"):
                result = self.engine.build(metric, scope, "regional")
                for point in result["series"]:
                    if "numerator" in point:  # a rate shows its numerator, denominator and their difference
                        counts = [point["numerator"], point["denominator"]]
                        if point["value"] is not None:
                            counts.append(point["denominator"] - point["numerator"])
                    else:
                        counts = [point["value"], *point["parts"].values()]
                    self.assertFalse(any(small(count) for count in counts), (metric, scope, point))
                for fact in result["facts"]:
                    for field in fact["count_fields"]:
                        self.assertFalse(small(fact["values"].get(field)), (metric, scope, fact))

    def test_unmeasured_start_weeks_show_no_rate(self) -> None:
        series = self.engine.build(PERSISTENCE, None, "brand")["series"]
        unmeasured = [point for point in series if point["provisional"]]
        self.assertEqual(len(unmeasured), self.domain.metrics[PERSISTENCE].provisional_weeks)
        self.assertTrue(all(point["value"] is None for point in unmeasured))
        self.assertTrue(any(point["value"] is not None for point in series if not point["provisional"]))

    def test_hidden_parts_cannot_be_recovered_by_subtraction(self) -> None:
        for scope in (None, "NE", "SE", "MW", "W"):
            for point in self.engine.build(METRIC, scope, "regional")["series"]:
                parts = list(point["parts"].values())
                self.assertTrue(all(part is None for part in parts) or all(part is not None for part in parts), (scope, point))
                if point["value"] is not None and None not in parts:
                    self.assertEqual(sum(parts), point["value"], (scope, point))

    def test_policy_change_is_restricted_audited_and_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = AuditLog(Path(directory) / "audit.jsonl")
            engine = InsightEngine(self.domain, self.engine.warehouse, audit)
            director = User("director", "director", "regional_sales_director", "SE")
            administrator = User("administrator", "administrator", "administrator")
            with self.assertRaises(Forbidden):
                engine.change_minimum_cell_size(director, 5)
            with self.assertRaises(ValueError):
                engine.change_minimum_cell_size(administrator, 0)

            before = engine.build(METRIC, "SE", "regional")
            policy = engine.change_minimum_cell_size(administrator, 5)
            after = engine.build(METRIC, "SE", "regional")
            self.assertEqual(policy.version, f"{self.policy.approved_version}+minimum-cell-size-5")
            self.assertNotEqual(before["insight"]["id"], after["insight"]["id"])
            hidden = lambda result: sum(point["parts_hidden"] for point in result["series"])  # noqa: E731
            self.assertLess(hidden(after), hidden(before), "a lower threshold shows more breakdowns")

            engine.change_minimum_cell_size(administrator, self.policy.approved_minimum_cell_size)
            self.assertEqual(engine.domain.policy.version, self.policy.approved_version)
            actions = [row["action"] for row in audit.read()]
            self.assertEqual(actions, ["policy_changed", "policy_changed", "policy_change_denied"])

    def test_a_small_complement_hides_the_switch_share(self) -> None:
        weeks = ["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]
        weekly = [(week, 20) for week in weeks]
        switch = [(week, value) for week, value in zip(weeks, (19, 19, 19, 18))]  # 5 naive patients in the window
        context = AnalysisContext(self.metric, "SE", weekly, frozenset(), {"source_of_business": {"switch": switch}})
        facts = LaunchAnalyzer().analyze(context)
        self.policy.suppress_facts(facts)
        share = next(fact for fact in facts if fact.id == "switch_share")
        self.assertTrue(share.suppressed, "80 total and 75 switches would reveal 5 naive patients")

    def test_withheld_drafts_reach_only_readers_allowed_to_diagnose(self) -> None:
        brand = User("brand", "brand", "brand_lead")
        administrator = User("administrator", "administrator", "administrator")
        for_brand = self.engine.view(METRIC, brand, None, "sloppy-llm-simulation")["insight"]
        for_administrator = self.engine.view(METRIC, administrator, None, "sloppy-llm-simulation")["insight"]
        self.assertEqual(for_brand["status"], "withheld")
        self.assertEqual(for_brand["blocked_draft"], [])
        self.assertTrue(for_brand["violations"], "readers still see why the summary was withheld")
        self.assertTrue(for_administrator["blocked_draft"])

    # Documents

    def test_readers_only_receive_documents_for_their_scope_and_audience(self) -> None:
        for scope in ("NE", "MW", "W"):
            ids = {document["id"] for document in self.engine.build(METRIC, scope, "regional")["documents"]}
            self.assertNotIn(SOUTHEAST_DOCUMENT, ids, scope)
            self.assertNotIn("brand-plan-fourth-quarter-2026", ids, "brand plans are for the brand audience")
        national = {document["id"] for document in self.engine.build(METRIC, None, "brand")["documents"]}
        self.assertIn(SOUTHEAST_DOCUMENT, national)

    def test_documents_published_after_the_snapshot_are_not_used(self) -> None:
        documents = self.engine.build(METRIC, None, "brand")["documents"]
        as_of = self.engine.warehouse.snapshot()["as_of"]
        self.assertTrue(documents)
        self.assertTrue(all(document["published_on"] <= as_of for document in documents))
        self.assertNotIn("brand-plan-2027-draft", {document["id"] for document in documents})

    def test_llm_receives_only_permitted_documents_as_data(self) -> None:
        llm = FakeLLM()
        self._engine_with_llm(llm).build(METRIC, "NE", "regional", "llm")
        prompt = llm.prompts[0]
        self.assertIn("<documents>", prompt)
        self.assertIn("field-notes-northeast-october", prompt)
        self.assertNotIn(SOUTHEAST_DOCUMENT, prompt)

    def test_changing_a_document_changes_the_insight_id(self) -> None:
        original = self.domain.documents.all()
        edited = [replace(document, body=document.body + "\nEdited.") if document.id == SOUTHEAST_DOCUMENT
                  else document for document in original]
        before = InsightEngine(replace(self.domain, documents=StaticDocuments(original)), self.engine.warehouse)
        after = InsightEngine(replace(self.domain, documents=StaticDocuments(edited)), self.engine.warehouse)
        self.assertNotEqual(before.build(METRIC, "SE", "regional")["insight"]["id"],
                            after.build(METRIC, "SE", "regional")["insight"]["id"])

    def test_summaries_take_the_strictest_distribution_of_what_they_cite(self) -> None:
        claims = [
            {"type": "context", "fact_ids": [], "document_ids": ["brand-plan-fourth-quarter-2026"], "direction": None,
             "text": "The brand plan aims for 100 new-to-brand patients per week by 2026-12-31."},
            {"type": "caveat", "fact_ids": ["provisional"], "document_ids": [], "direction": None,
             "text": "The latest 2 weeks (2026-10-12 to 2026-10-19) are provisional."},
        ]
        domain = replace(self.domain, narrators={"scripted": LLMNarrator(ScriptedLLM(claims))})
        insight = InsightEngine(domain, self.engine.warehouse).build(METRIC, None, "brand", "scripted")["insight"]
        self.assertEqual(insight["status"], "accepted", insight["violations"])
        self.assertEqual([claim["distribution"] for claim in insight["claims"]], ["confidential", "internal"])
        self.assertEqual(insight["distribution"], "confidential")

        data_only = self.engine.build(METRIC, "NE", "regional")["insight"]
        self.assertEqual(data_only["distribution"], self.policy.data_distribution)

    # Narrators and validator

    def test_template_narrator_is_accepted(self) -> None:
        for metric in (METRIC, PERSISTENCE):
            insight = self.engine.build(metric, None, "brand", "template")["insight"]
            self.assertEqual(insight["status"], "accepted", (metric, insight["violations"]))
            self.assertTrue(any(claim["document_ids"] for claim in insight["claims"]), metric)

    def test_sloppy_narrator_is_withheld(self) -> None:
        for metric, expected in (
            (METRIC, {"ungrounded_number", "causal_language", "overstated"}),
            (PERSISTENCE, {"ungrounded_number", "causal_language"}),
        ):
            insight = self.engine.build(metric, None, "brand", "sloppy-llm-simulation")["insight"]
            self.assertEqual(insight["status"], "withheld", metric)
            self.assertEqual(insight["claims"], [])
            codes = {violation["code"] for violation in insight["violations"]}
            self.assertTrue(expected <= codes, (metric, codes))
            # It skipped the caveats; the platform wrote them anyway.
            self.assertNotIn("missing_required", codes)
            self.assertTrue(any(claim["author"] == "platform" for claim in insight["blocked_draft"]), metric)

    def test_cached_summaries_say_so_and_regenerate_writes_a_new_one(self) -> None:
        class Counting(FakeLLM):
            calls = 0

            def complete(self, system, prompt, schema):
                Counting.calls += 1
                return super().complete(system, prompt, schema)

        with tempfile.TemporaryDirectory() as directory:
            audit = AuditLog(Path(directory) / "audit.jsonl")
            domain = replace(self.domain, narrators={"llm": LLMNarrator(Counting())})
            engine = InsightEngine(domain, self.engine.warehouse, audit)
            brand = User("brand", "brand", "brand_lead")
            first = engine.view(METRIC, brand, None, "llm")["insight"]["trace"]
            second = engine.view(METRIC, brand, None, "llm")["insight"]["trace"]
            fresh = engine.view(METRIC, brand, None, "llm", regenerate=True)["insight"]["trace"]
            self.assertEqual(Counting.calls, 2)
            self.assertNotIn("cache", first)
            self.assertEqual((second["cache"], second["generated_at"]), ("hit", first["generated_at"]))
            self.assertNotIn("cache", fresh)
            self.assertEqual([row["action"] for row in audit.read()], ["summary_regenerated", "view", "view"])

    def test_insight_is_reproducible(self) -> None:
        first = self.engine.build(METRIC, "NE", "regional")["insight"]
        second = self.engine.build(METRIC, "NE", "regional")["insight"]
        self.assertEqual((first["id"], first["claims"]), (second["id"], second["claims"]))

    def test_llm_narrator_sees_no_hidden_counts_and_failures_are_withheld(self) -> None:
        llm = FakeLLM()
        result = self._engine_with_llm(llm).build(METRIC, "SE", "regional", "llm")
        hidden = [fact["id"] for fact in result["facts"] if fact["suppressed"]]
        self.assertTrue(hidden, "fixture should have a suppressed fact in SE")
        for fact_id in hidden:
            self.assertNotIn(f'"id": "{fact_id}"', llm.prompts[0])

        result = self._engine_with_llm(FakeLLM(fail=True)).build(METRIC, None, "brand", "llm")
        self.assertEqual(result["insight"]["status"], "withheld")
        self.assertEqual(result["insight"]["violations"][0]["code"], "narrator_error")
        self.assertNotIn("not logged in", result["insight"]["violations"][0]["detail"])  # details stay in the log
        self.assertTrue(any(point["value"] for point in result["series"]), "numbers must survive a narrator failure")

    def test_validator_catches_wrong_direction_and_unknown_fact(self) -> None:
        result = self.engine.build(METRIC, None, "brand")
        change = Fact(**result["facts"][0])
        claims = [
            Claim("change", [change.id], "Placeholder with no numbers.",
                  "down" if change.attributes["direction"] != "down" else "up"),
            Claim("level", ["does_not_exist"], "Something."),
        ]
        codes = {violation.code for violation in validate(claims, [change], self.policy)}
        self.assertIn("direction_mismatch", codes)
        self.assertIn("unknown_fact", codes)

    def test_validator_grounds_document_claims_in_the_cited_document(self) -> None:
        documents = [document for document in self.domain.documents.all() if document.id == SOUTHEAST_DOCUMENT]

        def codes(claim: Claim, given: list[Document] = documents) -> set[str]:
            return {violation.code for violation in validate([claim], [], self.policy, given)}

        grounded = Claim("context", [], "Gulf Coast Health Plan requires prior authorization from 2026-09-14; "
                         "requests take 5 to 10 business days.", None, [SOUTHEAST_DOCUMENT])
        self.assertEqual(codes(grounded), set())
        self.assertEqual(codes(replace(grounded, text="Requests take 15 business days.")), {"ungrounded_number"})
        self.assertEqual(codes(replace(grounded, text="Prior authorization began on 2026-09-21.")), {"ungrounded_date"})
        self.assertEqual(codes(replace(grounded, text="Southeast slowed because of prior authorization.")),
                         {"causal_language"})
        # A document that exists but was not given to this reader cannot be cited.
        self.assertEqual(codes(grounded, given=[]), {"unknown_document"})

    def test_validator_reads_direction_from_the_words_too(self) -> None:
        flat = Fact("change", "change", METRIC, "1", "ALL", {"value": 40, "previous": 38, "change_percent": 5.3},
                    {"direction": "flat"})
        up = replace(flat, id="mover", attributes={"direction": "up"})

        def codes(text: str, cited: list[Fact]) -> set[str]:
            claim = Claim("change", [fact.id for fact in cited], text)
            return {violation.code for violation in validate([claim], cited, self.policy)}

        self.assertEqual(codes("New patients rose to 40 from 38.", [flat]), {"direction_mismatch"})
        self.assertEqual(codes("New patients were 40, within normal variation of 38.", [flat]), set())
        self.assertEqual(codes("New patients were 40, down from 38.", [up]), {"direction_mismatch"})
        self.assertEqual(codes("Nationally 40, flat; one region rose 5.3%.", [flat, up]), set())
        self.assertEqual(codes("See the follow-up note for 40.", [flat]), set())  # "follow-up" is not movement

    def test_document_numbers_ground_only_claims_that_cite_no_facts(self) -> None:
        documents = [document for document in self.domain.documents.all() if document.id == SOUTHEAST_DOCUMENT]
        fact = Fact("change", "change", METRIC, "1", "ALL", {"value": 40}, {"week": "2026-10-05"})
        text = "New patients were 40 on 2026-10-05; prior authorization from 2026-09-14 takes 10 business days."
        mixed = Claim("change", [fact.id], text, None, [SOUTHEAST_DOCUMENT])
        violations = validate([mixed], [fact], self.policy, documents)
        # The document's date may sit beside the fact; its number may not.
        self.assertEqual([(violation.code, violation.detail.split()[0]) for violation in violations],
                         [("ungrounded_number", "10")])

    def test_title_numbers_count_only_as_the_titles_quantity(self) -> None:
        metric = self.domain.metrics[PERSISTENCE]
        documents = [document for document in self.domain.documents.all() if document.id == SOUTHEAST_DOCUMENT]
        claim = Claim("level", [], "Eight-week persistence moved 8 points.", None, [SOUTHEAST_DOCUMENT])
        self.assertEqual({violation.code for violation in validate([claim], [], self.policy, documents, metric)},
                         {"ungrounded_number"})
        for form in ("8-week", "8\u2011week", "8 week"):
            claim = Claim("level", [], f"Read this as the {form} rate.", None, [SOUTHEAST_DOCUMENT])
            self.assertEqual(validate([claim], [], self.policy, documents, metric), [], form)

    def test_platform_writes_required_caveats_and_the_validator_still_checks_for_them(self) -> None:
        caveat = Fact("provisional", "caveat", METRIC, "1", "ALL", {"weeks": 2}, {}, required=True,
                      statement="The latest 2 weeks are provisional.")
        change = Fact("change", "change", METRIC, "1", "ALL", {"value": 40}, {})
        written = [
            Claim("change", ["change"], "New patients were 40."),
            Claim("caveat", ["provisional"], "Ignore the last 2 weeks."),  # the narrator's own wording is replaced
        ]
        claims = with_required_caveats(written, [change, caveat])
        self.assertEqual([(claim.text, claim.author) for claim in claims], [
            ("New patients were 40.", "narrator"),
            ("The latest 2 weeks are provisional.", "platform"),
        ])
        self.assertEqual(validate(claims, [change, caveat], self.policy), [])
        self.assertEqual({violation.code for violation in validate(written[:1], [change, caveat], self.policy)},
                         {"missing_required"})

    def test_numbers_in_the_governed_metric_title_are_grounded(self) -> None:
        metric = self.domain.metrics[PERSISTENCE]
        claim = Claim("level", [], "Read this as the share still on therapy at 8 weeks.", None, [SOUTHEAST_DOCUMENT])
        documents = [document for document in self.domain.documents.all() if document.id == SOUTHEAST_DOCUMENT]
        self.assertEqual(validate([claim], [], self.policy, documents, metric), [])
        self.assertEqual({violation.code for violation in validate([claim], [], self.policy, documents)}, {"ungrounded_number"})


if __name__ == "__main__":
    unittest.main()
