from __future__ import annotations

import unittest
from unittest.mock import patch

import providers
import providers_anthropic as anth
from provider_base import ProviderError
from providers import Provider


def _provider(**overrides) -> anth.AnthropicProvider:
    record = Provider(
        id="anthropic",
        name="Anthropic",
        type="anthropic",
        host="https://api.anthropic.com",
        api_key="sk-ant-test",
        api_key_env="ANTHROPIC_API_KEY",
        **overrides,
    )
    return anth.AnthropicProvider(record)


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# A history in exactly the shape KaraSession builds: system prompt, user turn,
# an assistant turn with two parallel tool calls, both results, then the
# trailing ephemeral clock.
KARA_HISTORY = [
    {"role": "system", "content": "SYSTEM PROMPT"},
    {"role": "user", "content": "check my repos"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-a",
                "type": "function",
                "function": {"name": "github_list_commits", "arguments": '{"repo": "x"}'},
            },
            {
                "id": "call-b",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "y"}'},
            },
        ],
    },
    {"role": "tool", "content": "commits", "tool_name": "github_list_commits", "tool_call_id": "call-a"},
    {"role": "tool", "content": "results", "tool_name": "web_search", "tool_call_id": "call-b"},
    {"role": "system", "content": "RUNTIME CLOCK", "ephemeral": True},
]


class SystemPromptSplitTests(unittest.TestCase):
    def test_system_prompt_is_lifted_out_of_messages(self) -> None:
        system, rest = anth._system_prompt_and_rest(KARA_HISTORY)
        self.assertEqual(system, "SYSTEM PROMPT")
        self.assertNotIn("system", [m.get("role") for m in rest])

    def test_the_ephemeral_clock_becomes_a_trailing_user_message(self) -> None:
        _, rest = anth._system_prompt_and_rest(KARA_HISTORY)
        self.assertEqual(rest[-1], {"role": "user", "content": "RUNTIME CLOCK"})

    def test_a_compaction_note_joins_the_system_prompt(self) -> None:
        system, _ = anth._system_prompt_and_rest(
            [
                {"role": "system", "content": "BASE"},
                {"role": "system", "content": "[earlier conversation compacted: 4]"},
            ]
        )
        self.assertIn("BASE", system)
        self.assertIn("compacted", system)


class MessageConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        _, rest = anth._system_prompt_and_rest(KARA_HISTORY)
        self.converted = anth.to_anthropic_messages(rest)

    def test_tool_results_are_coalesced_into_one_user_message(self) -> None:
        """Splitting them discourages the model from making parallel calls."""
        result_messages = [
            m
            for m in self.converted
            if m["role"] == "user"
            and isinstance(m["content"], list)
            and m["content"][0].get("type") == "tool_result"
        ]
        self.assertEqual(len(result_messages), 1)
        self.assertEqual(len(result_messages[0]["content"]), 2)

    def test_tool_results_keep_request_order_and_ids(self) -> None:
        blocks = next(
            m["content"]
            for m in self.converted
            if isinstance(m["content"], list)
            and m["content"][0].get("type") == "tool_result"
        )
        self.assertEqual([b["tool_use_id"] for b in blocks], ["call-a", "call-b"])
        self.assertEqual(blocks[0]["content"], "commits")

    def test_tool_calls_become_tool_use_blocks_with_parsed_input(self) -> None:
        assistant = next(m for m in self.converted if m["role"] == "assistant")
        uses = [b for b in assistant["content"] if b["type"] == "tool_use"]
        self.assertEqual([u["name"] for u in uses], ["github_list_commits", "web_search"])
        # Kara stores a JSON string; Anthropic needs an object.
        self.assertEqual(uses[0]["input"], {"repo": "x"})

    def test_failed_tool_results_are_marked(self) -> None:
        converted = anth.to_anthropic_messages(
            [{"role": "tool", "content": "boom", "tool_call_id": "c1", "is_error": True}]
        )
        self.assertTrue(converted[0]["content"][0]["is_error"])

    def test_successful_results_carry_no_error_flag(self) -> None:
        converted = anth.to_anthropic_messages(
            [{"role": "tool", "content": "fine", "tool_call_id": "c1"}]
        )
        self.assertNotIn("is_error", converted[0]["content"][0])

    def test_plain_turns_pass_through(self) -> None:
        converted = anth.to_anthropic_messages(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        )
        self.assertEqual(converted[0], {"role": "user", "content": "hi"})
        self.assertEqual(
            converted[1], {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
        )

    def test_separate_tool_batches_stay_separate(self) -> None:
        converted = anth.to_anthropic_messages(
            [
                {"role": "tool", "content": "a", "tool_call_id": "1"},
                {"role": "assistant", "content": "thinking"},
                {"role": "tool", "content": "b", "tool_call_id": "2"},
            ]
        )
        result_msgs = [
            m for m in converted if isinstance(m["content"], list)
            and m["content"][0].get("type") == "tool_result"
        ]
        self.assertEqual(len(result_msgs), 2)


class ToolSchemaConversionTests(unittest.TestCase):
    def test_schemas_are_flattened(self) -> None:
        out = anth.to_anthropic_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {"type": "object", "properties": {"p": {"type": "string"}}},
                    },
                }
            ]
        )
        self.assertEqual(out[0]["name"], "read_file")
        self.assertEqual(out[0]["description"], "Read a file")
        self.assertEqual(out[0]["input_schema"]["properties"], {"p": {"type": "string"}})
        self.assertNotIn("function", out[0])

    def test_real_kara_schemas_all_convert(self) -> None:
        from tools import registry

        out = anth.to_anthropic_tools(registry.TOOL_SCHEMAS)
        self.assertEqual(len(out), len(registry.TOOL_SCHEMAS))
        for tool in out:
            self.assertTrue(tool["name"])
            self.assertIn("input_schema", tool)

    def test_no_tools_is_empty(self) -> None:
        self.assertEqual(anth.to_anthropic_tools(None), [])


class ResponseMappingTests(unittest.TestCase):
    def test_text_response_maps_with_usage(self) -> None:
        result = anth.chat_result_from_anthropic(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1200, "output_tokens": 40},
            }
        )
        self.assertEqual(result.content, "Hello")
        self.assertEqual(result.usage.prompt_tokens, 1200)
        self.assertEqual(result.usage.completion_tokens, 40)
        self.assertEqual(result.finish_reason, "end_turn")

    def test_tool_use_blocks_map_to_tool_calls(self) -> None:
        result = anth.chat_result_from_anthropic(
            {
                "content": [
                    {"type": "text", "text": "Looking..."},
                    {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"query": "k"}},
                ],
                "stop_reason": "tool_use",
            }
        )
        self.assertTrue(result.wants_tools)
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.tool_calls[0].id, "tu_1")
        self.assertEqual(result.tool_calls[0].arguments, {"query": "k"})
        self.assertEqual(result.content, "Looking...")

    def test_a_refusal_produces_readable_text(self) -> None:
        """A refusal is HTTP 200 with empty content — don't report 'no response'."""
        result = anth.chat_result_from_anthropic(
            {"content": [], "stop_reason": "refusal", "usage": {"input_tokens": 10}}
        )
        self.assertEqual(result.finish_reason, "refusal")
        self.assertIn("declined", result.content)

    def test_thinking_blocks_are_preserved(self) -> None:
        blocks = [
            {"type": "thinking", "thinking": "", "signature": "abc123"},
            {"type": "text", "text": "Answer"},
        ]
        result = anth.chat_result_from_anthropic({"content": blocks, "stop_reason": "end_turn"})
        self.assertEqual(result.provider_content, blocks)
        self.assertEqual(result.content, "Answer")

    def test_missing_usage_is_zero(self) -> None:
        result = anth.chat_result_from_anthropic(
            {"content": [{"type": "text", "text": "x"}]}
        )
        self.assertFalse(result.usage.reported)

    def test_an_error_payload_raises(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            anth.chat_result_from_anthropic({"error": {"message": "model_not_found"}})
        self.assertIn("model_not_found", str(ctx.exception))

    def test_a_non_object_payload_raises(self) -> None:
        with self.assertRaises(ProviderError):
            anth.chat_result_from_anthropic("nope")


class ThinkingRoundTripTests(unittest.TestCase):
    """Thinking blocks must replay byte-identically or the next turn can 400."""

    BLOCKS = [
        {"type": "thinking", "thinking": "", "signature": "sig-xyz"},
        {"type": "text", "text": "Done"},
    ]

    def test_blocks_survive_result_to_message(self) -> None:
        result = anth.chat_result_from_anthropic(
            {"content": self.BLOCKS, "stop_reason": "end_turn"}
        )
        self.assertEqual(result.to_message()["provider_content"], self.BLOCKS)

    def test_preserved_blocks_are_replayed_verbatim(self) -> None:
        message = anth.chat_result_from_anthropic(
            {"content": self.BLOCKS, "stop_reason": "end_turn"}
        ).to_message()
        converted = anth.to_anthropic_messages([message])
        self.assertEqual(converted[0]["content"], self.BLOCKS)

    def test_a_full_round_trip_through_sqlite_is_identical(self) -> None:
        import tempfile
        from pathlib import Path

        import config
        import session_db

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(config, "BRAIN_DIR", root),
                patch.object(session_db, "DB_PATH", root / "state.db"),
            ):
                session_db._initialized = False
                session_db.init_db()
                session_db.ensure_session("s", "cli", "anthropic", "claude-opus-5")

                original = anth.chat_result_from_anthropic(
                    {"content": self.BLOCKS, "stop_reason": "end_turn"}
                ).to_message()
                session_db.append_message("s", original)
                restored = session_db.load_messages("s")
                session_db._initialized = False

        self.assertEqual(restored[0]["provider_content"], self.BLOCKS)
        # And the replayed request body matches the original response blocks.
        self.assertEqual(
            anth.to_anthropic_messages(restored)[0]["content"], self.BLOCKS
        )


class TransportTests(unittest.TestCase):
    def _capture(self, payload=None):
        captured: dict = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp(
                payload
                or {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
            )

        with patch("providers_anthropic.httpx.post", fake_post):
            result = _provider().chat(
                "claude-opus-5",
                KARA_HISTORY,
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "read_file", "description": "d", "parameters": {}},
                    }
                ],
            )
        return captured, result

    def test_request_shape(self) -> None:
        captured, result = self._capture()
        self.assertEqual(result.content, "ok")
        self.assertTrue(captured["url"].endswith("/v1/messages"))
        self.assertEqual(captured["headers"]["x-api-key"], "sk-ant-test")
        self.assertEqual(captured["headers"]["anthropic-version"], anth.API_VERSION)
        self.assertNotIn("Authorization", captured["headers"])

    def test_max_tokens_is_always_sent(self) -> None:
        captured, _ = self._capture()
        import config

        self.assertEqual(captured["json"]["max_tokens"], config.MAX_OUTPUT_TOKENS)

    def test_system_is_top_level_and_cached(self) -> None:
        captured, _ = self._capture()
        self.assertEqual(captured["json"]["system"][0]["text"], "SYSTEM PROMPT")
        self.assertEqual(
            captured["json"]["system"][0]["cache_control"], {"type": "ephemeral"}
        )
        self.assertNotIn("system", [m["role"] for m in captured["json"]["messages"]])

    def test_tools_are_flattened_in_the_request(self) -> None:
        captured, _ = self._capture()
        self.assertEqual(captured["json"]["tools"][0]["name"], "read_file")
        self.assertIn("input_schema", captured["json"]["tools"][0])

    def test_no_sampling_parameters_are_sent(self) -> None:
        captured, _ = self._capture()
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, captured["json"])

    def test_rate_limits_and_overload_are_retryable(self) -> None:
        for status in (429, 529, 500):
            with self.subTest(status=status):

                def fake_post(url, headers=None, json=None, timeout=None):
                    request = httpx.Request("POST", url)
                    response = httpx.Response(status, text="slow down", request=request)
                    raise httpx.HTTPStatusError("e", request=request, response=response)

                import httpx

                with patch("providers_anthropic.httpx.post", fake_post):
                    with self.assertRaises(ProviderError) as ctx:
                        _provider().chat("claude-opus-5", KARA_HISTORY)
                self.assertTrue(ctx.exception.retryable)

    def test_client_errors_are_not_retryable(self) -> None:
        import httpx

        def fake_post(url, headers=None, json=None, timeout=None):
            request = httpx.Request("POST", url)
            response = httpx.Response(401, text="bad key", request=request)
            raise httpx.HTTPStatusError("e", request=request, response=response)

        with patch("providers_anthropic.httpx.post", fake_post):
            with self.assertRaises(ProviderError) as ctx:
                _provider().chat("claude-opus-5", KARA_HISTORY)
        self.assertFalse(ctx.exception.retryable)

    def test_connection_failures_are_retryable(self) -> None:
        import httpx

        def fake_post(url, headers=None, json=None, timeout=None):
            raise httpx.ConnectError("refused")

        with patch("providers_anthropic.httpx.post", fake_post):
            with self.assertRaises(ProviderError) as ctx:
                _provider().chat("claude-opus-5", KARA_HISTORY)
        self.assertTrue(ctx.exception.retryable)

    def test_embeddings_are_reported_as_unavailable(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            _provider().embed("text")
        self.assertIn("Ollama embeddings", str(ctx.exception))


class WiringTests(unittest.TestCase):
    def test_the_adapter_is_built_for_the_anthropic_type(self) -> None:
        record = Provider(
            id="anthropic", name="Anthropic", type="anthropic",
            host="https://api.anthropic.com",
        )
        self.assertIsInstance(providers.to_chat_provider(record), anth.AnthropicProvider)

    def test_env_discovery_creates_the_provider(self) -> None:
        with patch.dict(
            providers.os.environ, {"ANTHROPIC_API_KEY": "sk-ant-x"}, clear=False
        ):
            found = {d["id"]: d for d in providers._discover_provider_defs_from_env()}
        self.assertIn("anthropic", found)
        self.assertEqual(found["anthropic"]["type"], "anthropic")
        self.assertEqual(found["anthropic"]["api_key_env"], "ANTHROPIC_API_KEY")

    def test_no_key_means_no_provider(self) -> None:
        env = {k: v for k, v in providers.os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(providers.os.environ, env, clear=True):
            found = {d["id"] for d in providers._discover_provider_defs_from_env()}
        self.assertNotIn("anthropic", found)

    def test_the_default_model_is_current(self) -> None:
        import models

        self.assertEqual(models.PROVIDER_DEFAULT_MODELS["anthropic"], "claude-opus-5")


if __name__ == "__main__":
    unittest.main()
