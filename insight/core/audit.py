"""Audit log: who saw which metric, under which definition, with what outcome.

Owner: platform engineer. A JSONL file here; Postgres (append-only table) in production.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **event: Any) -> None:
        line = json.dumps({"timestamp": datetime.now(UTC).isoformat(timespec="seconds"), **event})
        with self._lock, self.path.open("a") as file:
            file.write(line + "\n")

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines)]
