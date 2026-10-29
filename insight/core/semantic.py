"""Governed metric definitions and how they run against the warehouse.

Owner: platform engineer builds this engine. The definitions it loads
(domains/*/metrics/*.toml) belong to whoever governs the metric; at Celldex,
the Director of Commercial Data Science.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import tomllib
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Metric:
    name: str
    version: str
    title: str
    description: str
    owner: str
    approved_on: str
    entity: str
    dimensions: tuple[str, ...]
    parameters: dict[str, Any]
    # One implementation per warehouse dialect; tests hold them to the same answers.
    event_sql: dict[str, str]
    # "count": distinct entities per group. "rate": the share of them whose
    # `numerator` column is 1, reported as a percent.
    measure: str = "count"
    numerator: str | None = None
    # Breakdowns the analyzer receives; `stack_by` is the one the chart stacks.
    breakdowns: tuple[str, ...] = ()
    stack_by: str | None = None
    # The newest weeks are incomplete, and why. A rate is not shown for them at all.
    provisional_weeks: int = 0
    provisional_reason: str = ""
    week_label: str = "Week"

    @classmethod
    def load(cls, path: Path) -> Metric:
        raw = tomllib.loads(path.read_text())
        return cls(
            name=raw["name"],
            version=raw["version"],
            title=raw["title"],
            description=" ".join(raw["description"].split()),
            owner=raw["owner"],
            approved_on=raw["approved_on"],
            entity=raw["entity"],
            dimensions=tuple(raw["dimensions"]),
            parameters=dict(raw.get("parameters", {})),
            event_sql=dict(raw["event_sql"]),
            measure=raw.get("measure", "count"),
            numerator=raw.get("numerator"),
            breakdowns=tuple(raw.get("breakdowns", ())),
            stack_by=raw.get("stack_by"),
            provisional_weeks=raw.get("provisional_weeks", 0),
            provisional_reason=raw.get("provisional_reason", ""),
            week_label=raw.get("week_label", "Week"),
        )

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "owner": self.owner,
            "approved_on": self.approved_on,
            "dimensions": list(self.dimensions),
            "parameters": self.parameters,
            "measure": self.measure,
            "stack_by": self.stack_by,
            "provisional_weeks": self.provisional_weeks,
            "provisional_reason": self.provisional_reason,
            "week_label": self.week_label,
        }


class Warehouse(Protocol):
    """Plugin boundary: SQLite locally, Databricks SQL in the cloud profile."""

    dialect: str

    def query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]: ...

    def snapshot(self) -> dict[str, str]: ...


class SqliteWarehouse:
    """Local stand-in for the warehouse. Read-only by construction."""

    dialect = "sqlite"

    def __init__(self, path: Path):
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; run: python3 -m insight.domains.pharmaceutical.synthetic_warehouse")
        self.path = path

    def query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters)]

    def snapshot(self) -> dict[str, str]:
        return self.query("SELECT as_of, first_week, source_hash FROM snapshot", {})[0]



class CachedWarehouse:
    """Caches query results per data snapshot.

    Results can only change when the warehouse publishes a new snapshot, so the
    snapshot's source hash is part of every cache key and a new snapshot empties
    the cache. The snapshot row itself is re-read at most every `snapshot_time_to_live_seconds`
    seconds, which bounds how long a newly published snapshot takes to show.
    This assumes loads publish the snapshot row last, after the data is in place.
    """

    def __init__(self, inner: Warehouse, snapshot_time_to_live_seconds: float = 60, maximum_entries: int = 512):
        self.inner, self.dialect = inner, inner.dialect
        self.snapshot_time_to_live_seconds = snapshot_time_to_live_seconds
        self.maximum_entries = maximum_entries
        self._snapshot: dict[str, str] | None = None
        self._checked = 0.0
        self._results: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            if self._snapshot and time.monotonic() - self._checked < self.snapshot_time_to_live_seconds:
                return dict(self._snapshot)
        fresh = self.inner.snapshot()
        with self._lock:
            if fresh != self._snapshot:
                self._results.clear()
            self._snapshot, self._checked = fresh, time.monotonic()
            return dict(fresh)

    def query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        key = json.dumps([self.snapshot()["source_hash"], sql, parameters], sort_keys=True, default=str)
        with self._lock:
            if key in self._results:
                self._results.move_to_end(key)
                self.hits += 1
                return [dict(row) for row in self._results[key]]
        rows = self.inner.query(sql, parameters)
        with self._lock:
            self.misses += 1
            self._results[key] = rows
            while len(self._results) > self.maximum_entries:
                self._results.popitem(last=False)
        return [dict(row) for row in rows]


def run_metric(
    warehouse: Warehouse,
    metric: Metric,
    group_by: list[str],
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate a metric. Callers name dimensions; they never supply SQL.

    Each row has `value`, the count of distinct entities, and for a rate metric also
    `numerator`, the count of those whose numerator column is 1.
    """
    filters = filters or {}
    unknown = [dimension for dimension in [*group_by, *filters] if dimension not in metric.dimensions]
    if unknown:
        raise ValueError(f"metric {metric.name} has no dimension(s) {unknown}")
    event_sql = metric.event_sql.get(warehouse.dialect)
    if event_sql is None:
        raise ValueError(f"metric {metric.name} has no {warehouse.dialect} implementation")
    columns = ", ".join(group_by)
    where = " AND ".join(f"{dimension} = :filter_{dimension}" for dimension in filters)
    sql = (
        f"SELECT {columns}, COUNT(DISTINCT {metric.entity}) AS value"
        + (f", COUNT(DISTINCT CASE WHEN {metric.numerator} = 1 THEN {metric.entity} END) AS numerator"
           if metric.measure == "rate" else "")
        + " "
        + f"FROM ({event_sql}) AS events "
        + (f"WHERE {where} " if where else "")
        + f"GROUP BY {columns} ORDER BY {columns}"
    )
    parameters = {**metric.parameters, **{f"filter_{dimension}": value for dimension, value in filters.items()}}
    return warehouse.query(sql, parameters)


def open_warehouse(sqlite_path: Path) -> Warehouse:
    """INSIGHT_WAREHOUSE=databricks selects Databricks SQL; otherwise the local SQLite file.

    Either way, results are cached per snapshot.
    """
    if os.environ.get("INSIGHT_WAREHOUSE") == "databricks":
        from insight.core.databricks import DatabricksWarehouse

        return CachedWarehouse(DatabricksWarehouse.from_environment())
    return CachedWarehouse(SqliteWarehouse(sqlite_path))
