"""LLM access for narrators. Owner: platform engineer.

Narrators ask for structured output against a JSON schema; this module decides how
the call is made, bounds it (timeout, retries), and reports what it cost. ClaudeAPI
calls the Claude API through the Anthropic SDK; foundry.py does the same for a
Foundry deployment. Both sit behind the same StructuredLLM interface.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol


try:  # optional: only the Claude API adapter needs the SDK
    import anthropic
except ImportError:
    anthropic = None


class LLMError(Exception):
    pass


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    model: str  # the model that wrote the answer
    models: dict[str, dict[str, float]]  # every model the call used, with tokens and cost
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_milliseconds: int

    def trace(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key != "data"}


class StructuredLLM(Protocol):
    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult: ...


# List prices in USD per million tokens, for the cost estimate in the trace.
CLAUDE_PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
}


class ClaudeAPI:
    """Calls the Claude API with structured JSON output (output_config.format).

    Credentials come from the environment (ANTHROPIC_API_KEY), never from code.
    The SDK retries 429, 5xx and connection errors itself; this adapter turns what
    is left, plus refusals and truncation, into LLMError. Server-side fallback is on:
    if a safety classifier declines, the API re-runs the request on a fallback model
    in the same call, and the trace records which model answered.
    """

    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "low", timeout_seconds: float = 120,
                 max_tokens: int = 16000, client: Any = None):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        if client is None:
            if anthropic is None:
                raise LLMError("the anthropic package is not installed (sudo apt install python3-anthropic)")
            client = anthropic.Anthropic(timeout=timeout_seconds)
        self.client = client

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
        started = time.perf_counter()
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                # Thinking is left adaptive (Sonnet 5.5 and Opus 5.5 reject "disabled"); effort
                # is the control, and "low" suits a short summary.
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": _supported_schema(schema)}},
            )
        except _SDK_ERRORS as error:
            raise LLMError(_describe(error)) from error
        duration = round((time.perf_counter() - started) * 1000)

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMError(f"Claude declined the request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude stopped at max_tokens ({self.max_tokens}) before finishing")
        text = next((block.text for block in response.content if block.type == "text"), None)
        try:
            data = json.loads(text or "")
        except json.JSONDecodeError as error:
            raise LLMError("Claude returned no structured output") from error

        usage = response.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        prices = CLAUDE_PRICES.get(self.model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
        cost = (usage.input_tokens * prices["input"] + cache_write * prices["cache_write"]
                + cache_read * prices["cache_read"] + usage.output_tokens * prices["output"]) / 1e6
        input_tokens = usage.input_tokens + cache_write + cache_read
        return LLMResult(
            data=data,
            model=response.model,
            models={response.model: {"input_tokens": input_tokens, "output_tokens": usage.output_tokens,
                                     "cost_usd": round(cost, 6)}},
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            duration_milliseconds=duration,
        )

    @classmethod
    def from_environment(cls) -> ClaudeAPI | None:
        """None when no API key is configured or the SDK is missing, so the domain leaves it out."""
        if anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        return cls(model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"))


# Constraints the Claude API's structured outputs reject with a 400. The prompt states
# them in words instead (for example "at most six short claims").
_UNSUPPORTED_SCHEMA_KEYWORDS = {"maxItems"}


def _supported_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {key: _supported_schema(value) for key, value in schema.items() if key not in _UNSUPPORTED_SCHEMA_KEYWORDS}
    if isinstance(schema, list):
        return [_supported_schema(item) for item in schema]
    return schema


# Most specific first: what is still failing after the SDK's own retries.
_SDK_ERRORS: tuple[type[BaseException], ...] = (
    (anthropic.APIStatusError, anthropic.APIConnectionError) if anthropic else ()
)


def _describe(error: BaseException) -> str:
    if isinstance(error, anthropic.AuthenticationError):
        return "Claude API rejected the key (check ANTHROPIC_API_KEY)"
    if isinstance(error, anthropic.RateLimitError):
        return "Claude API rate limit reached; try again shortly"
    if isinstance(error, anthropic.BadRequestError):
        return f"Claude API rejected the request: {error.message[:300]}"
    if isinstance(error, anthropic.APIStatusError):
        return f"Claude API error {error.status_code}: {error.message[:300]}"
    return f"Claude API unreachable: {error}"
