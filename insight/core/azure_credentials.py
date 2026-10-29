"""Azure access tokens for the adapters that call Azure services. Owner: platform engineer.

Locally the developer's `az login` is used. On AKS the pod runs as a user-assigned
managed identity through Workload Identity: the cluster mounts a service account token,
and it is exchanged with Entra for an access token, with no secret anywhere. The adapters
only need a callable that returns a bearer token; azure_token() picks the right one.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable


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


class WorkloadIdentityToken:
    """Bearer token for one Azure resource from AKS Workload Identity, refreshed before it expires.

    The Workload Identity webhook sets AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_AUTHORITY_HOST and
    AZURE_FEDERATED_TOKEN_FILE. The file holds a short-lived service account token that the kubelet
    rotates, so it is read again on every exchange.
    """

    def __init__(self, resource: str, environment: dict[str, str] | None = None):
        environment = os.environ if environment is None else environment
        self.resource = resource
        self.client_id = environment["AZURE_CLIENT_ID"]
        self.token_file = environment["AZURE_FEDERATED_TOKEN_FILE"]
        authority = environment.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/").rstrip("/")
        self.url = f"{authority}/{environment['AZURE_TENANT_ID']}/oauth2/v2.0/token"
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if time.time() > self._expires - 300:
                with open(self.token_file) as file:
                    assertion = file.read().strip()
                form = urllib.parse.urlencode({
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "scope": f"{self.resource}/.default",
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                }).encode()
                try:
                    with urllib.request.urlopen(urllib.request.Request(self.url, data=form), timeout=30) as response:
                        data = json.load(response)
                except urllib.error.HTTPError as error:
                    detail = error.read().decode(errors="replace")[:300]
                    raise CredentialError(f"Entra refused the workload identity token: {error.code} {detail}") from None
                self._token, self._expires = data["access_token"], time.time() + float(data["expires_in"])
            return self._token


def azure_token(resource: str) -> Callable[[], str]:
    """Workload Identity when the pod has one, otherwise the developer's az login."""
    if os.environ.get("AZURE_FEDERATED_TOKEN_FILE"):
        return WorkloadIdentityToken(resource)
    return AzureCliToken(resource)
