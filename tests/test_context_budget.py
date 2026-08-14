from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from memory import context_budget
from providers import ollama_client
from providers.registry import Provider


class ContextWindowIsSentToOllamaTests(unittest.TestCase):
    """Ollama silently truncates past num_ctx, so it must always be explicit."""

    def _capture_payload(self) -> dict:
        provider = Provider(
            id="ollama-local", name="Local", type="ollama", host="http://x"
        )
        captured: dict = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"message": {"role": "assistant", "content": "ok"}}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json or {})
            return _Resp()

        with patch.object(ollama_client.httpx, "post", fake_post):
            ollama_client.chat(provider, "m", [{"role": "user", "content": "hi"}])
        return captured

    def test_num_ctx_is_always_sent(self) -> None:
        payload = self._capture_payload()
        self.assertIn("num_ctx", payload["options"])
        self.assertEqual(payload["options"]["num_ctx"], config.MODEL_CONTEXT_TOKENS)

    def test_temperature_is_still_sent(self) -> None:
        self.assertIn("temperature", self._capture_payload()["options"])


class EstimationTests(unittest.TestCase):
    def test_empty_text_costs_nothing(self) -> None:
        self.assertEqual(context_budget.estimate_tokens(""), 0)

    def test_estimate_grows_with_length(self) -> None:
        short = context_budget.estimate_tokens("a" * 100)
        long = context_budget.estimate_tokens("a" * 1000)
        self.assertLess(short, long)

    def test_no_schemas_cost_nothing(self) -> None:
        self.assertEqual(context_budget.estimate_schema_tokens(None), 0)
        self.assertEqual(context_budget.estimate_schema_tokens([]), 0)

    def test_message_tokens_include_tool_calls(self) -> None:
        plain = {"role": "assistant", "content": "hello"}
        with_calls = {
            "role": "assistant",
            "content": "hello",
            "tool_calls": [{"id": "1", "function": {"name": "read_file", "arguments": {}}}],
        }
        self.assertLess(
            context_budget.estimate_message_tokens(plain),
            context_budget.estimate_message_tokens(with_calls),
        )

    def test_messages_total_is_the_sum_of_its_parts(self) -> None:
        messages = [
            {"role": "system", "content": "x" * 400},
            {"role": "user", "content": "y" * 200},
        ]
        self.assertEqual(
            context_budget.estimate_messages_tokens(messages),
            sum(context_budget.estimate_message_tokens(m) for m in messages),
        )


class WindowCheckTests(unittest.TestCase):
    SYSTEM = "s" * 8000  # ~2000 tokens
    SCHEMAS = [{"function": {"name": "t", "description": "d" * 4000}}]

    def test_window_smaller_than_overhead_is_flagged_hard(self) -> None:
        warning = context_budget.check_configured_window(
            self.SYSTEM, self.SCHEMAS, window=1024
        )
        self.assertIsNotNone(warning)
        self.assertIn("silently truncate", warning)

    def test_tight_window_warns_with_remaining_room(self) -> None:
        overhead = context_budget.fixed_overhead_tokens(self.SYSTEM, self.SCHEMAS)
        warning = context_budget.check_configured_window(
            self.SYSTEM, self.SCHEMAS, window=int(overhead * 1.5)
        )
        self.assertIsNotNone(warning)
        self.assertIn("%", warning)

    def test_comfortable_window_is_silent(self) -> None:
        self.assertIsNone(
            context_budget.check_configured_window(self.SYSTEM, self.SCHEMAS, window=100_000)
        )

    def test_real_kara_prompt_fits_the_shipped_default(self) -> None:
        """The shipped default must comfortably hold Kara's own overhead."""
        import kara
        from tools import registry

        warning = context_budget.check_configured_window(
            kara.get_system_instruction("telegram"),
            registry.schemas_for_groups(set(registry.ALWAYS_ON)),
            window=config.MODEL_CONTEXT_TOKENS,
        )
        self.assertIsNone(warning, warning)

    def test_ollama_default_window_would_be_flagged(self) -> None:
        """Regression guard for the bug: 4096 is Ollama's default and is too small."""
        import kara
        from tools import registry

        warning = context_budget.check_configured_window(
            kara.get_system_instruction("telegram"),
            registry.schemas_for_groups(set(registry.ALWAYS_ON)),
            window=4096,
        )
        self.assertIsNotNone(warning)


if __name__ == "__main__":
    unittest.main()
