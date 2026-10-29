"""A public deployment: anyone can sign in as any role, so nothing a visitor does may cost money
or change what other visitors see. Checked through the HTTP server, as a visitor would hit it."""

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

from insight.domains.pharmaceutical import synthetic_warehouse
from insight.server import App, make_handler

ADMINISTRATOR = "prototype-administrator"


class PublicModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        synthetic_warehouse.build(Path(cls.directory.name) / "warehouse.db", seed=7)
        cls.servers = {}
        # A Claude key is configured, as it could be by mistake; public mode must still leave it out.
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "configured-by-mistake"}):
            for public in (True, False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(Path(cls.directory.name), public)))
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

    def test_no_writer_that_calls_a_paid_model(self) -> None:
        _, catalog = self.request(True, "/api/catalog")
        self.assertTrue(catalog["public"])
        self.assertEqual([narrator["name"] for narrator in catalog["narrators"]], ["template", "sloppy-llm-simulation"])
        _, private = self.request(False, "/api/catalog")
        self.assertIn("claude-sonnet-5-5", [narrator["name"] for narrator in private["narrators"]])

    def test_even_an_administrator_cannot_change_shared_state_or_spend(self) -> None:
        _, me = self.request(True, "/api/me")
        self.assertEqual((me["can_edit_policy"], me["can_run_evaluations"]), (False, False))
        status, _ = self.request(True, "/api/policy/minimum-cell-size", {"value": 5})
        self.assertEqual(status, 403)
        status, _ = self.request(True, "/api/evaluations/run", {"narrator": "template"})
        self.assertEqual(status, 403)
        _, catalog = self.request(True, "/api/catalog")
        self.assertEqual(catalog["policy"]["minimum_cell_size"], 11)

    def test_reading_still_works(self) -> None:
        status, result = self.request(True, "/api/metrics/new_to_brand_patients?narrator=template")
        self.assertEqual((status, result["insight"]["status"]), (200, "accepted"))
        _, me = self.request(True, "/api/me")
        self.assertTrue(me["can_read_audit"])


if __name__ == "__main__":
    unittest.main()
