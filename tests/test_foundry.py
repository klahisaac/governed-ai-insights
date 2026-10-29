"""The Foundry adapter, with a fake transport instead of the network.

Run: python3 -m unittest tests.test_foundry -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from insight.core.engine import InsightEngine
from insight.core.foundry import FoundryChat
from insight.core.llm import LLMError
from insight.core.semantic import SqliteWarehouse
from insight.domains import pharmaceutical
from insight.domains.pharmaceutical import synthetic_warehouse
from insight.domains.pharmaceutical.narrators import CLAIMS_SCHEMA, FoundryNarrator


def reply(content: str | None, finish_reason: str = "stop", refusal: str | None = None) -> dict:
    return {
        "model": "gpt-5-mini-2025-08-07",
        "choices": [{"finish_reason": finish_reason, "message": {"content": content, "refusal": refusal}}],
        "usage": {"prompt_tokens": 4000, "completion_tokens": 500},
    }


class FoundryChatTest(unittest.TestCase):
    def chat(self, response: dict) -> tuple[FoundryChat, list]:
        calls = []

        def transport(url, body, headers, timeout):
            calls.append((url, body, headers))
            return response

        chat = FoundryChat("https://example.cognitiveservices.azure.com/", "gpt-5-mini", lambda: "token",
                           0.25, 2.0, transport=transport)
        return chat, calls

    def test_request_uses_strict_schema_and_entra_token_and_reports_cost(self) -> None:
        chat, calls = self.chat(reply(json.dumps({"claims": []})))
        result = chat.complete("system", "prompt", CLAIMS_SCHEMA)
        url, body, headers = calls[0]
        self.assertEqual(url, "https://example.cognitiveservices.azure.com/openai/v1/chat/completions")
        self.assertEqual(body["model"], "gpt-5-mini")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], CLAIMS_SCHEMA)
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertEqual(result.data, {"claims": []})
        self.assertAlmostEqual(result.cost_usd, (4000 * 0.25 + 500 * 2.0) / 1e6)
        self.assertEqual((result.input_tokens, result.output_tokens), (4000, 500))

    def test_incomplete_refused_or_unparseable_answers_are_errors(self) -> None:
        for response in (reply('{"claims": [', finish_reason="length"), reply(None, refusal="no"), reply("not json")):
            chat, _ = self.chat(response)
            with self.subTest(response=response), self.assertRaises(LLMError):
                chat.complete("system", "prompt", CLAIMS_SCHEMA)

    def test_foundry_summaries_go_through_the_same_validator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "warehouse.db"
            synthetic_warehouse.build(database, seed=7)
            inflated = {"claims": [{"type": "change", "fact_ids": ["week_over_week_change"], "document_ids": [],
                                    "direction": "up", "text": "New-to-brand patients surged to 500."}]}
            chat, _ = self.chat(reply(json.dumps(inflated)))
            narrator = FoundryNarrator(chat)
            domain = replace(pharmaceutical.load(), narrators={narrator.name: narrator})
            insight = InsightEngine(domain, SqliteWarehouse(database)).build(
                "new_to_brand_patients", None, "brand", narrator.name)["insight"]
        self.assertEqual(narrator.name, "foundry-gpt-5-mini")
        self.assertEqual(insight["status"], "withheld")
        self.assertIn("ungrounded_number", {violation["code"] for violation in insight["violations"]})
        self.assertEqual(insight["trace"]["model"], "gpt-5-mini-2025-08-07")


if __name__ == "__main__":
    unittest.main()
