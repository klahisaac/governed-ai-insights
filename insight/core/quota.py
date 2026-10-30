"""A daily cap on paid model calls. Owner: platform engineer.

A public deployment lets anyone sign in as any role, so every model call (a summary that is not
cached, a regeneration, each case of a gate run) counts against one shared budget. The count is
kept in a file next to the audit log, so a restart does not reset it. Days are UTC.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from insight.core.llm import LLMError, LLMResult, StructuredLLM


class QuotaExceeded(LLMError):
    pass


class DailyQuota:
    def __init__(self, limit: int, path: Path, today: Callable[[], str] | None = None):
        self.limit = limit
        self.path = path
        self._today = today or (lambda: datetime.now(UTC).date().isoformat())
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            stored = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            stored = {}
        return stored if stored.get("day") == self._today() else {"day": self._today(), "used": 0}

    def status(self) -> dict[str, Any]:
        with self._lock:
            stored = self._read()
        return {"day": stored["day"], "used": stored["used"], "limit": self.limit}

    def take(self, calls: int = 1) -> None:
        """Counts calls before they are made; raises QuotaExceeded, counting nothing, when they do not fit."""
        with self._lock:
            stored = self._read()
            if stored["used"] + calls > self.limit:
                raise QuotaExceeded(f"today's model call quota ({self.limit}) is used up; it resets at 00:00 UTC")
            stored["used"] += calls
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(stored))


class QuotaLLM:
    """A StructuredLLM that takes one call from the quota before each request. Everything else
    (model, deployment) is the wrapped client's, so narrator names stay the same."""

    def __init__(self, llm: StructuredLLM, quota: DailyQuota):
        self._llm = llm
        self._quota = quota

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
        self._quota.take()
        return self._llm.complete(system, prompt, schema)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)
