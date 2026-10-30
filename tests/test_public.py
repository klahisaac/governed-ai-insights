"""A public deployment: anyone can sign in as any role, so everything stays on and paid model calls
share a daily quota. Checked through the HTTP server, as a visitor would hit it, and on the quota itself."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from insight.core.llm import LLMResult
from insight.core.quota import DailyQuota, QuotaExceeded, QuotaLLM
from insight.domains.pharmaceutical import synthetic_warehouse
from insight.server import App, make_handler

ADMINISTRATOR = "prototype-administrator"


class PublicModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        for public in (True, False):
            (Path(cls.directory.name) / str(public)).mkdir()
            synthetic_warehouse.build(Path(cls.directory.name) / str(public) / "warehouse.db", seed=7)
        cls.servers = {}
        # A Claude key is configured (never called here); the public server gets a small quota.
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "not-a-real-key"}):
            for public in (True, False):
                app = App(Path(cls.directory.name) / str(public), public, model_calls_per_day=5 if public else 0)
                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                cls.servers[public] = server

    @classmethod
    def tearDownClass(cls) -> None:
        for server in cls.servers.values():
            server.shutdown()
            server.server_close()
        cls.directory.cleanup()

    def request(self, public: bool, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.servers[public].server_port}{path}"
        headers = {"Authorization": f"Bearer {ADMINISTRATOR}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_model_writers_are_listed_with_the_quota(self) -> None:
        _, catalog = self.request(True, "/api/catalog")
        self.assertTrue(catalog["public"])
        self.assertIn("claude-sonnet-5-5", [narrator["name"] for narrator in catalog["narrators"]])
        self.assertEqual((catalog["model_calls"]["used"], catalog["model_calls"]["limit"]), (0, 5))
        _, private = self.request(False, "/api/catalog")
        self.assertIsNone(private["model_calls"])

    def test_everything_is_on_but_a_gate_run_must_fit_in_the_quota(self) -> None:
        _, me = self.request(True, "/api/me")
        self.assertEqual((me["can_edit_policy"], me["can_run_evaluations"]), (True, True))
        # Ten cases do not fit in five calls: refused before the model is called once.
        status, body = self.request(True, "/api/evaluations/run", {"narrator": "claude-sonnet-5-5"})
        self.assertEqual(status, 429, body)
        _, catalog = self.request(True, "/api/catalog")
        self.assertEqual(catalog["model_calls"]["used"], 0)
        # The free writer costs nothing and is not counted.
        status, record = self.request(True, "/api/evaluations/run", {"narrator": "template"})
        self.assertEqual((status, record["total"]), (200, 10))
        _, catalog = self.request(True, "/api/catalog")
        self.assertEqual(catalog["model_calls"]["used"], 0)

    def test_reading_still_works(self) -> None:
        status, result = self.request(True, "/api/metrics/new_to_brand_patients?narrator=template")
        self.assertEqual((status, result["insight"]["status"]), (200, "accepted"))
        _, me = self.request(True, "/api/me")
        self.assertTrue(me["can_read_audit"])


class FakeLLM:
    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, prompt: str, schema: dict) -> LLMResult:
        self.calls += 1
        return LLMResult({"claims": []}, self.model, {}, 0, 0, 0.0, 0)


class DailyQuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "model_calls.json"
        self.day = "2026-10-29"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def quota(self) -> DailyQuota:
        return DailyQuota(2, self.path, today=lambda: self.day)

    def test_calls_past_the_limit_are_refused_without_reaching_the_model(self) -> None:
        llm = FakeLLM()
        capped = QuotaLLM(llm, self.quota())
        self.assertEqual(capped.model, "fake-model")  # narrators name themselves after the wrapped client
        capped.complete("", "", {})
        capped.complete("", "", {})
        with self.assertRaises(QuotaExceeded):
            capped.complete("", "", {})
        self.assertEqual(llm.calls, 2)

    def test_the_count_survives_a_restart_and_resets_the_next_day(self) -> None:
        self.quota().take(2)
        with self.assertRaises(QuotaExceeded):
            self.quota().take()  # a new process reads the same file
        self.day = "2026-10-30"
        self.quota().take()
        self.assertEqual(self.quota().status(), {"day": "2026-10-30", "used": 1, "limit": 2})


if __name__ == "__main__":
    unittest.main()
