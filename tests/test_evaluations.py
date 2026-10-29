"""Gate runs are recorded with the sentences that failed, and only readers of every region see them.

Run: python3 -m unittest tests.test_evaluations -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insight.core.engine import InsightEngine
from insight.core.evaluations import EvaluationStore
from insight.core.policy import User
from insight.core.semantic import SqliteWarehouse
from insight.domains import pharmaceutical
from insight.domains.pharmaceutical import synthetic_warehouse
from insight.evaluation_gate import evaluate


class EvaluationRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(cls.temporary_directory.name) / "warehouse.db"
        synthetic_warehouse.build(database, seed=7)
        cls.domain = pharmaceutical.load()
        cls.engine = InsightEngine(cls.domain, SqliteWarehouse(database))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_a_run_records_every_case_and_the_sentence_behind_each_failure(self) -> None:
        run = evaluate(self.domain, self.engine, "sloppy-llm-simulation")
        regions = len(self.domain.scope_members(self.engine.warehouse))
        self.assertEqual(run["total"], len(self.domain.metrics) * (1 + regions))
        self.assertEqual(run["passed"], 0)
        failure = next(failure for case in run["cases"] for failure in case["failures"]
                       if failure["code"] == "causal_language")
        self.assertIn(failure["detail"].split('"')[1], failure["sentence"])
        # Required caveats are written by the platform, so even this writer cannot leave them out.
        self.assertFalse([failure for case in run["cases"] for failure in case["failures"]
                          if failure["code"] == "missing_required"])

    def test_the_store_keeps_history_and_serves_the_latest_run_per_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EvaluationStore(Path(directory) / "runs.jsonl")
            first = evaluate(self.domain, self.engine, "template")
            store.append(first)
            store.append(evaluate(self.domain, self.engine, "sloppy-llm-simulation"))
            store.append({**first, "run_id": "later"})
            latest = store.latest()
            self.assertEqual(set(latest), {"template", "sloppy-llm-simulation"})
            self.assertEqual(latest["template"]["run_id"], "later")
            self.assertEqual(latest["template"]["passed"], latest["template"]["total"])
            self.assertEqual(len((Path(directory) / "runs.jsonl").read_text().splitlines()), 3)

    def test_only_administrators_may_run_the_gate_from_the_page(self) -> None:
        policy = self.domain.policy
        self.assertTrue(policy.can(User("administrator", "administrator", "administrator"), "evaluation:run"))
        self.assertFalse(policy.can(User("brand", "brand", "brand_lead"), "evaluation:run"))

    def test_roles_see_only_their_cards(self) -> None:
        policy = self.domain.policy
        director = User("director", "director", "regional_sales_director", "SE")
        brand = User("brand", "brand", "brand_lead")
        administrator = User("administrator", "administrator", "administrator")
        self.assertEqual(policy.views(director), ["chart", "summary", "definition", "documents"])
        self.assertTrue(policy.can_view(brand, "facts"))
        self.assertFalse(policy.can_view(brand, "evaluation"))
        self.assertTrue(all(policy.can_view(administrator, view) for view in
                            ("policy", "provenance", "evaluation", "audit")))

    def test_only_readers_of_every_region_may_see_evaluations(self) -> None:
        policy = self.domain.policy
        self.assertTrue(policy.sees_every_member(User("brand", "brand", "brand_lead")))
        self.assertFalse(policy.sees_every_member(User("director", "director", "regional_sales_director", "SE")))


if __name__ == "__main__":
    unittest.main()
