"""Evaluation runs: what the gate measured for each summary writer. Owner: platform engineer.

Every gate run is appended as one JSON line, so the history stays and the page can
show the latest run per writer. A JSONL file here; a Delta table in production.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class EvaluationStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def append(self, run: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a") as file:
            file.write(json.dumps(run) + "\n")

    def latest(self) -> dict[str, dict[str, Any]]:
        """The most recent run for each summary writer, by writer name."""
        if not self.path.exists():
            return {}
        runs: dict[str, dict[str, Any]] = {}
        for line in self.path.read_text().splitlines():
            run = json.loads(line)
            runs[run["narrator"]] = run
        return runs

