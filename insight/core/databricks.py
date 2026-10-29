"""Databricks SQL warehouse adapter. Owner: platform engineer.

Talks to the SQL Statement Execution API over HTTPS with the standard library, so
there is no driver to install. Metric SQL keeps its :named parameters; they are
sent as typed statement parameters, never formatted into the SQL text.

Read-only is enforced by grants, not by this code: in production the app's
service principal holds only USE CATALOG / USE SCHEMA / SELECT on the schema.

Configuration (environment):
  DATABRICKS_HOST          https://adb-....azuredatabricks.net
  DATABRICKS_WAREHOUSE_ID  SQL warehouse id
  DATABRICKS_CATALOG       for example my_workspace_catalog
  DATABRICKS_SCHEMA        for example pharmaceutical
  DATABRICKS_TOKEN         optional; without it Workload Identity (on AKS) or az login is used
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from insight.core.azure_credentials import azure_token

# The Azure Databricks first-party application; tokens for it are accepted by every workspace.
AZURE_DATABRICKS_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"

_INTEGER_TYPES = {"BYTE", "SHORT", "INT", "LONG"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE"}


class WarehouseError(RuntimeError):
    pass


def _statement_parameter(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"name": name, "value": str(value).lower(), "type": "BOOLEAN"}
    if isinstance(value, int):
        return {"name": name, "value": str(value), "type": "INT"}
    if isinstance(value, float):
        return {"name": name, "value": repr(value), "type": "DOUBLE"}
    return {"name": name, "value": str(value), "type": "STRING"}


def _cast(type_name: str, value: str | None) -> Any:
    if value is None:
        return None
    if type_name in _INTEGER_TYPES:
        return int(value)
    if type_name in _FLOAT_TYPES:
        return float(value)
    return value  # dates and timestamps stay ISO strings, the same as SQLite returns them


class DatabricksSQL:
    """Runs one statement and returns rows as dicts. Shared by the warehouse and loaders."""

    def __init__(
        self,
        host: str,
        warehouse_id: str,
        catalog: str,
        schema: str,
        token: Callable[[], str],
        timeout_seconds: float = 120,
    ):
        self.host = host.rstrip("/")
        if not self.host.startswith("https://"):
            self.host = f"https://{self.host}"
        self.warehouse_id, self.catalog, self.schema = warehouse_id, catalog, schema
        self.token, self.timeout_seconds = token, timeout_seconds

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.host}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raise WarehouseError(f"{method} {path}: HTTP {error.code}: {error.read().decode()[:500]}") from None

    def execute(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        response = self._call("POST", "/api/2.0/sql/statements", {
            "warehouse_id": self.warehouse_id,
            "catalog": self.catalog,
            "schema": self.schema,
            "statement": sql,
            "parameters": [_statement_parameter(name, value) for name, value in (parameters or {}).items()],
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
            # A stopped serverless warehouse takes a few seconds to resume.
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
        })
        statement_id = response["statement_id"]
        deadline = time.monotonic() + self.timeout_seconds
        while response["status"]["state"] in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                self._call("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                raise WarehouseError(f"statement {statement_id} timed out after {self.timeout_seconds}s")
            time.sleep(1)
            response = self._call("GET", f"/api/2.0/sql/statements/{statement_id}")
        state = response["status"]["state"]
        if state != "SUCCEEDED":
            message = response["status"].get("error", {}).get("message", "")
            raise WarehouseError(f"statement {statement_id} {state}: {message[:500]}")

        columns = response.get("manifest", {}).get("schema", {}).get("columns", [])
        result = response.get("result", {})
        rows = list(result.get("data_array", []))
        while result.get("next_chunk_index") is not None:
            result = self._call(
                "GET", f"/api/2.0/sql/statements/{statement_id}/result/chunks/{result['next_chunk_index']}"
            )
            rows.extend(result.get("data_array", []))
        return [{column["name"]: _cast(column["type_name"], value) for column, value in zip(columns, row)} for row in rows]

    @classmethod
    def from_environment(cls) -> DatabricksSQL:
        missing = [name for name in ("DATABRICKS_HOST", "DATABRICKS_WAREHOUSE_ID", "DATABRICKS_CATALOG",
                               "DATABRICKS_SCHEMA") if not os.environ.get(name)]
        if missing:
            raise WarehouseError(f"set {', '.join(missing)}")
        static = os.environ.get("DATABRICKS_TOKEN")
        return cls(
            os.environ["DATABRICKS_HOST"],
            os.environ["DATABRICKS_WAREHOUSE_ID"],
            os.environ["DATABRICKS_CATALOG"],
            os.environ["DATABRICKS_SCHEMA"],
            (lambda: static) if static else azure_token(AZURE_DATABRICKS_RESOURCE),
        )


class DatabricksWarehouse:
    """The Warehouse plugin for Databricks SQL."""

    dialect = "databricks"

    def __init__(self, sql: DatabricksSQL):
        self.sql = sql

    def query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        return self.sql.execute(sql, parameters)

    def snapshot(self) -> dict[str, str]:
        rows = self.query("SELECT as_of, first_week, source_hash FROM snapshot", {})
        return {key: str(value) for key, value in rows[0].items()}

    @classmethod
    def from_environment(cls) -> DatabricksWarehouse:
        return cls(DatabricksSQL.from_environment())
