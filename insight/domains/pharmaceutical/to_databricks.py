"""Copy the synthetic warehouse into Databricks (Unity Catalog).

Owner (at Celldex): data engineering. Real claims land through vendor pipelines;
this script only exists so the prototype can run against a real Databricks warehouse.
It replaces the four tables in DATABRICKS_CATALOG.DATABRICKS_SCHEMA, writing the
snapshot table last so a reader never sees a new snapshot before its data.

Run: python3 -m insight.domains.pharmaceutical.to_databricks
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from insight.core.databricks import DatabricksSQL
from insight.domains.pharmaceutical.synthetic_warehouse import DEFAULT_PATH

# Column order matches the SQLite tables built by synthetic_warehouse.py; snapshot goes last.
TABLES = {
    "regions": [("code", "STRING"), ("name", "STRING")],
    "prescribers": [("prescriber_id", "STRING"), ("region", "STRING"), ("specialty", "STRING")],
    "fills": [("fill_id", "BIGINT"), ("patient_id", "STRING"), ("prescriber_id", "STRING"),
              ("product", "STRING"), ("fill_date", "DATE")],
    "snapshot": [("as_of", "DATE"), ("first_week", "DATE"), ("source_hash", "STRING")],
}
ROWS_PER_INSERT = 2000


def _literal(value: object, type_name: str) -> str:
    if value is None:
        return "NULL"
    if type_name == "BIGINT":
        return str(int(value))
    text = "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    return f"DATE{text}" if type_name == "DATE" else text


def load(sql: DatabricksSQL, source: Path = DEFAULT_PATH) -> dict[str, int]:
    sql.execute(f"CREATE SCHEMA IF NOT EXISTS `{sql.catalog}`.`{sql.schema}` "
                "COMMENT 'Synthetic prototype data: fictional brand, no real patients'")
    counts = {}
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as connection:
        for table, columns in TABLES.items():
            names = ", ".join(name for name, _ in columns)
            definition = ", ".join(f"{name} {type_name} NOT NULL" for name, type_name in columns)
            sql.execute(f"CREATE OR REPLACE TABLE {table} ({definition})")
            rows = connection.execute(f"SELECT {names} FROM {table}").fetchall()
            for start in range(0, len(rows), ROWS_PER_INSERT):
                values = ",\n".join(
                    "(" + ", ".join(_literal(value, type_name) for value, (_, type_name) in zip(row, columns)) + ")"
                    for row in rows[start : start + ROWS_PER_INSERT]
                )
                sql.execute(f"INSERT INTO {table} ({names}) VALUES\n{values}")
            loaded = sql.execute(f"SELECT COUNT(*) AS row_count FROM {table}")[0]["row_count"]
            if loaded != len(rows):
                raise RuntimeError(f"{table}: loaded {loaded} rows, expected {len(rows)}")
            counts[table] = loaded
    return counts


if __name__ == "__main__":
    client = DatabricksSQL.from_environment()
    for name, row_count in load(client).items():
        print(f"{client.catalog}.{client.schema}.{name}: {row_count} rows")
