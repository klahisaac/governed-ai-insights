"""The Claude API adapter, with a fake client in place of the Anthropic SDK.

Run: python3 -m unittest tests.test_claude_api -v
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from insight.core.llm import ClaudeAPI, LLMError
from insight.domains.pharmaceutical.narrators import CLAIMS_SCHEMA


def response(text: str | None, stop_reason: str = "end_turn", category: str | None = None) -> SimpleNamespace:
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        model="claude-sonnet-5-5",
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if stop_reason == "refusal" else None,
        content=content,
        usage=SimpleNamespace(input_tokens=3000, output_tokens=600, cache_creation_input_tokens=0,
                              cache_read_input_tokens=1000),
    )


class FakeClient:
    def __init__(self, reply: SimpleNamespace):
        self.reply, self.requests = reply, []
        self.beta = SimpleNamespace(messages=self)

    def create(self, **request):
        self.requests.append(request)
        return self.reply


class ClaudeAPITest(unittest.TestCase):
    def test_request_asks_for_the_schema_and_low_effort_and_reports_cost(self) -> None:
        client = FakeClient(response(json.dumps({"claims": []})))
        result = ClaudeAPI(client=client).complete("system", "prompt", CLAIMS_SCHEMA)
        request = client.requests[0]
        self.assertEqual(request["model"], "claude-sonnet-5-5")
        self.assertEqual(request["system"], "system")
        sent = request["output_config"]["format"]["schema"]
        self.assertEqual(request["output_config"]["effort"], "low")
        self.assertNotIn("maxItems", sent["properties"]["claims"])  # rejected by the API; the prompt says it instead
        self.assertEqual(sent["properties"]["claims"]["items"], CLAIMS_SCHEMA["properties"]["claims"]["items"])
        self.assertNotIn("thinking", request)  # Sonnet 5.5 rejects disabled thinking; effort is the control
        self.assertEqual((request["betas"], request["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(result.data, {"claims": []})
        self.assertEqual((result.input_tokens, result.output_tokens), (4000, 600))
        self.assertAlmostEqual(result.cost_usd, (3000 * 2.00 + 1000 * 0.20 + 600 * 10.00) / 1e6)

    def test_refusal_truncation_and_missing_json_are_errors(self) -> None:
        for reply in (response(None, "refusal", "bio"), response('{"claims": [', "max_tokens"), response("plain text")):
            with self.subTest(stop_reason=reply.stop_reason), self.assertRaises(LLMError):
                ClaudeAPI(client=FakeClient(reply)).complete("system", "prompt", CLAIMS_SCHEMA)


if __name__ == "__main__":
    unittest.main()
