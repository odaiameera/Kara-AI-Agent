from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import kara
from provider_base import (
    ChatResult,
    ProviderError,
    ToolCall,
    Usage,
    chat_result_from_ollama,
    parse_tool_arguments,
    tool_calls_from_openai_shape,
)
from tools import registry


class ArgumentParsingTests(unittest.TestCase):
    def test_dict_passes_through(self) -> None:
        self.assertEqual(parse_tool_arguments({"a": 1}), {"a": 1})

    def test_json_string_is_parsed(self) -> None:
        self.assertEqual(parse_tool_arguments('{"a": 1}'), {"a": 1})

    def test_malformed_json_becomes_empty(self) -> None:
        self.assertEqual(parse_tool_arguments("{not json"), {})

    def test_non_object_json_becomes_empty(self) -> None:
        self.assertEqual(parse_tool_arguments("[1, 2]"), {})

    def test_none_becomes_empty(self) -> None:
        self.assertEqual(parse_tool_arguments(None), {})


class ToolCallWireTests(unittest.TestCase):
    def test_arguments_serialize_as_a_json_string(self) -> None:
        """Codex stringifies whatever it finds, so a dict must not be stored raw."""
        wire = ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"}).to_wire()
        raw = wire["function"]["arguments"]
        self.assertIsInstance(raw, str)
        self.assertEqual(json.loads(raw), {"path": "a.txt"})

    def test_wire_shape_carries_id_and_name(self) -> None:
        wire = ToolCall(id="c1", name="read_file").to_wire()
        self.assertEqual(wire["id"], "c1")
        self.assertEqual(wire["type"], "function")
        self.assertEqual(wire["function"]["name"], "read_file")

    def test_calls_without_a_name_are_dropped(self) -> None:
        calls = tool_calls_from_openai_shape(
            [{"id": "c1", "function": {"name": ""}}, {"id": "c2", "function": {"name": "ok"}}]
        )
        self.assertEqual([c.name for c in calls], ["ok"])

    def test_missing_ids_are_synthesized_so_results_can_correlate(self) -> None:
        calls = tool_calls_from_openai_shape([{"function": {"name": "read_file"}}])
        self.assertTrue(calls[0].id)


class OllamaMappingTests(unittest.TestCase):
    def test_text_response_maps_with_usage(self) -> None:
        result = chat_result_from_ollama(
            {
                "message": {"role": "assistant", "content": "Hello"},
                "prompt_eval_count": 120,
                "eval_count": 8,
                "done_reason": "stop",
            }
        )
        self.assertEqual(result.content, "Hello")
        self.assertEqual(result.usage.prompt_tokens, 120)
        self.assertEqual(result.usage.completion_tokens, 8)
        self.assertEqual(result.usage.total_tokens, 128)
        self.assertTrue(result.usage.reported)
        self.assertEqual(result.finish_reason, "stop")
        self.assertFalse(result.wants_tools)

    def test_tool_response_maps_and_sets_finish_reason(self) -> None:
        result = chat_result_from_ollama(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "web_search", "arguments": {"query": "x"}},
                        }
                    ],
                }
            }
        )
        self.assertTrue(result.wants_tools)
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.tool_calls[0].arguments, {"query": "x"})

    def test_missing_usage_reports_zero_not_a_crash(self) -> None:
        result = chat_result_from_ollama({"message": {"role": "assistant", "content": "hi"}})
        self.assertEqual(result.usage.total_tokens, 0)
        self.assertFalse(result.usage.reported)

    def test_empty_but_well_formed_message_is_allowed(self) -> None:
        """An empty assistant turn is valid; only a broken shape is an error."""
        result = chat_result_from_ollama({"message": {"role": "assistant", "content": ""}})
        self.assertEqual(result.content, "")
        self.assertEqual(result.role, "assistant")

    def test_missing_message_object_raises(self) -> None:
        with self.assertRaises(ProviderError):
            chat_result_from_ollama({"error": "model not found"})

    def test_non_dict_payload_raises(self) -> None:
        with self.assertRaises(ProviderError):
            chat_result_from_ollama("boom")


class PhantomUserMessageTests(unittest.TestCase):
    """Regression: a malformed response used to be persisted as a user turn."""

    def test_to_message_always_carries_an_explicit_role(self) -> None:
        self.assertEqual(ChatResult().to_message()["role"], "assistant")
        self.assertEqual(ChatResult(content="x").to_message()["role"], "assistant")

    def test_malformed_payload_never_reaches_history(self) -> None:
        class _BrokenProvider:
            def chat(self, model, messages, tools=None):
                # Exactly the shape that used to become `{}` and then a user turn.
                return chat_result_from_ollama({"unexpected": True})

        session = kara.KaraSession.__new__(kara.KaraSession)
        session.session_key = "cli:test"
        session.channel = "cli"
        session.model = "m"
        session.provider = _BrokenProvider()
        session.messages = []
        session.allowed_tool_names = None
        session.active_groups = set(registry.ALWAYS_ON)
        session._persist = Mock()

        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            self.assertRaises(ProviderError),
        ):
            session.handle_message("hello")

        # The user's own message is there; no phantom assistant/user turn follows.
        roles = [m.get("role") for m in session.messages]
        self.assertEqual(roles, ["user"])

    def test_empty_response_returns_the_no_response_marker(self) -> None:
        class _SilentProvider:
            def chat(self, model, messages, tools=None):
                return ChatResult(content="")

        session = kara.KaraSession.__new__(kara.KaraSession)
        session.session_key = "cli:test"
        session.channel = "cli"
        session.model = "m"
        session.provider = _SilentProvider()
        session.messages = []
        session.allowed_tool_names = None
        session.active_groups = set(registry.ALWAYS_ON)
        session._persist = Mock()

        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
        ):
            reply = session.handle_message("hello")

        self.assertEqual(reply, "(No response from model.)")
        # Stored as an assistant turn, not a user one.
        self.assertEqual([m["role"] for m in session.messages], ["user", "assistant"])


class UsageTests(unittest.TestCase):
    def test_totals_add_up(self) -> None:
        self.assertEqual(Usage(10, 5).total_tokens, 15)

    def test_unreported_usage_is_falsy(self) -> None:
        self.assertFalse(Usage().reported)


if __name__ == "__main__":
    unittest.main()
