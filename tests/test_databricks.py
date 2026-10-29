"""The Databricks implementation of every metric gives the SQLite answers.

Needs a loaded workspace (python3 -m insight.domains.pharmaceutical.to_databricks) and
the DATABRICKS_* settings; skipped otherwise. Run:
  INSIGHT_DATABRICKS_TESTS=1 python3 -m unittest tests.test_databricks -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from insight.core.databricks import DatabricksWarehouse
from insight.core.engine import InsightEngine
from insight.core.semantic import SqliteWarehouse, run_metric
from insight.domains import pharmaceutical
from insight.domains.pharmaceutical import synthetic_warehouse


@unittest.skipUnless(os.environ.get("INSIGHT_DATABRICKS_TESTS"), "set INSIGHT_DATABRICKS_TESTS=1")
class DatabricksParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(cls.temporary_directory.name) / "warehouse.db"
        synthetic_warehouse.build(database, seed=7)
        cls.local = SqliteWarehouse(database)
        cls.remote = DatabricksWarehouse.from_environment()
        cls.domain = pharmaceutical.load()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_same_snapshot(self) -> None:
        self.assertEqual(self.local.snapshot(), self.remote.snapshot())

    def test_metrics_match(self) -> None:
        for metric in self.domain.metrics.values():
            for group_by in (["week"], ["week", "region"], ["week", "source_of_business"]):
                for filters in ({}, {"region": "SE"}):
                    with self.subTest(metric=metric.name, group_by=group_by, filters=filters):
                        self.assertEqual(
                            run_metric(self.local, metric, group_by, filters),
                            run_metric(self.remote, metric, group_by, filters),
                        )

    def test_same_insight(self) -> None:
        local = InsightEngine(self.domain, self.local).build("new_to_brand_patients", None, "brand")
        remote = InsightEngine(self.domain, self.remote).build("new_to_brand_patients", None, "brand")
        self.assertEqual(
            (local["series"], local["facts"], local["insight"]["id"]),
            (remote["series"], remote["facts"], remote["insight"]["id"]),
        )


if __name__ == "__main__":
    unittest.main()
