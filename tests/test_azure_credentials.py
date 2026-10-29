"""Workload Identity: the service account token is exchanged for an Entra token, cached, and
read again from its file each time it is exchanged (the kubelet rotates it)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from insight.core.azure_credentials import AzureCliToken, CredentialError, WorkloadIdentityToken, azure_token


class FakeEntra(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, str]]] = []
    status = 200

    def do_POST(self) -> None:
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        FakeEntra.requests.append((self.path, {key: values[0] for key, values in form.items()}))
        body = (json.dumps({"access_token": f"entra-{len(FakeEntra.requests)}", "expires_in": 3600})
                if FakeEntra.status == 200 else '{"error": "invalid_client"}').encode()
        self.send_response(FakeEntra.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments) -> None:
        pass


class WorkloadIdentityTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeEntra.requests, FakeEntra.status = [], 200
        self.server = HTTPServer(("127.0.0.1", 0), FakeEntra)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.directory = tempfile.TemporaryDirectory()
        self.token_file = Path(self.directory.name) / "token"
        self.token_file.write_text("service-account-token-1\n")
        self.environment = {
            "AZURE_CLIENT_ID": "client",
            "AZURE_TENANT_ID": "tenant",
            "AZURE_AUTHORITY_HOST": f"http://127.0.0.1:{self.server.server_port}/",
            "AZURE_FEDERATED_TOKEN_FILE": str(self.token_file),
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def test_exchanges_the_service_account_token_and_caches_the_result(self) -> None:
        token = WorkloadIdentityToken("databricks-resource", self.environment)
        self.assertEqual((token(), token()), ("entra-1", "entra-1"))
        path, form = FakeEntra.requests[0]
        self.assertEqual(path, "/tenant/oauth2/v2.0/token")
        self.assertEqual(form["client_id"], "client")
        self.assertEqual(form["scope"], "databricks-resource/.default")
        self.assertEqual(form["client_assertion"], "service-account-token-1")
        self.assertEqual(form["grant_type"], "client_credentials")

        # Near expiry it exchanges again, with the token the kubelet has rotated in since.
        self.token_file.write_text("service-account-token-2\n")
        token._expires = 0
        self.assertEqual(token(), "entra-2")
        self.assertEqual(FakeEntra.requests[1][1]["client_assertion"], "service-account-token-2")

    def test_a_refusal_is_a_credential_error(self) -> None:
        FakeEntra.status = 401
        with self.assertRaises(CredentialError) as raised:
            WorkloadIdentityToken("resource", self.environment)()
        self.assertIn("invalid_client", str(raised.exception))

    def test_azure_token_uses_workload_identity_only_inside_a_pod(self) -> None:
        with mock.patch.dict("os.environ", self.environment, clear=True):
            self.assertIsInstance(azure_token("resource"), WorkloadIdentityToken)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(azure_token("resource"), AzureCliToken)


if __name__ == "__main__":
    unittest.main()
