"""Azure access tokens for the adapters that call Azure services. Owner: platform engineer.

The prototype borrows the developer's `az login`. A deployed app swaps this for its
managed identity; the adapters only need a callable that returns a bearer token.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time


class CredentialError(RuntimeError):
    pass


class AzureCliToken:
    """Bearer token for one Azure resource from `az account get-access-token`, refreshed before it expires."""

    def __init__(self, resource: str):
        self.resource = resource
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if time.time() > self._expires - 300:
                process = subprocess.run(
                    ["az", "account", "get-access-token", "--resource", self.resource, "-o", "json"],
                    capture_output=True, text=True, timeout=60,
                )
                if process.returncode != 0:
                    raise CredentialError(f"az could not get a token for {self.resource} (run az login): "
                                          f"{process.stderr.strip()}")
                data = json.loads(process.stdout)
                self._token, self._expires = data["accessToken"], float(data["expires_on"])
            return self._token
