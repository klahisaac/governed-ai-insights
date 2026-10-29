"""Microsoft Foundry adapter: a model deployment called over HTTPS. Owner: platform engineer.

Uses the OpenAI-compatible v1 chat completions API of an Azure AI Foundry resource
with strict JSON schema output, so it is a drop-in StructuredLLM. Authentication is
an Entra ID token (role "Cognitive Services OpenAI User" on the resource); there is
no API key in the app. Standard library only.

Configuration (environment):
  FOUNDRY_ENDPOINT     https://<resource>.cognitiveservices.azure.com
  FOUNDRY_DEPLOYMENT   deployment name, for example gpt-5-mini
  FOUNDRY_INPUT_PRICE_PER_MILLION, FOUNDRY_OUTPUT_PRICE_PER_MILLION
                       list prices in USD, for the cost estimate in the trace
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
from insight.core.llm import LLMError, LLMResult

COGNITIVE_SERVICES_RESOURCE = "https://cognitiveservices.azure.com"


class FoundryChat:
    """StructuredLLM over a Foundry deployment. The schema is enforced by the service (strict mode)."""

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        token: Callable[[], str],
        input_price_per_million: float,
        output_price_per_million: float,
        reasoning_effort: str = "low",
        timeout_seconds: float = 120,
        max_output_tokens: int = 4000,
        transport: Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]] | None = None,
    ):
        self.url = endpoint.rstrip("/") + "/openai/v1/chat/completions"
        self.deployment = deployment
        self.token = token
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self._transport = transport or _post_json

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
        body = {
            "model": self.deployment,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "summary", "strict": True, "schema": schema}},
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": self.max_output_tokens,
        }
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        started = time.perf_counter()
        response = self._transport(self.url, body, headers, self.timeout_seconds)
        duration = round((time.perf_counter() - started) * 1000)

        choice = (response.get("choices") or [{}])[0]
        if choice.get("finish_reason") not in ("stop", None):
            raise LLMError(f"Foundry stopped early: {choice.get('finish_reason')}")
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise LLMError(f"Foundry refused: {message['refusal'][:200]}")
        try:
            data = json.loads(message.get("content") or "")
        except json.JSONDecodeError as error:
            raise LLMError("Foundry returned no structured output") from error

        usage = response.get("usage") or {}
        input_tokens, output_tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        cost = (input_tokens * self.input_price_per_million + output_tokens * self.output_price_per_million) / 1e6
        model = response.get("model") or self.deployment
        return LLMResult(
            data=data,
            model=model,
            models={model: {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": round(cost, 6)}},
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            duration_milliseconds=duration,
        )

    @classmethod
    def from_environment(cls) -> FoundryChat | None:
        """None when Foundry is not configured, so the domain simply leaves it out."""
        if not os.environ.get("FOUNDRY_ENDPOINT"):
            return None
        return cls(
            os.environ["FOUNDRY_ENDPOINT"],
            os.environ.get("FOUNDRY_DEPLOYMENT", "gpt-5-mini"),
            azure_token(COGNITIVE_SERVICES_RESOURCE),
            float(os.environ.get("FOUNDRY_INPUT_PRICE_PER_MILLION", "0")),
            float(os.environ.get("FOUNDRY_OUTPUT_PRICE_PER_MILLION", "0")),
        )


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise LLMError(f"Foundry HTTP {error.code}: {error.read().decode()[:300]}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise LLMError(f"Foundry unreachable: {error}") from None
