from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import kara


class _FakeProvider:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.tool_batches: list[list[dict] | None] = []

    def chat(self, model, messages, tools=None):
        self.tool_batches.append(tools)
        if self.responses:
            return self.responses.pop(0)
        return {"message": {"role": "assistant", "content": "ok"}}


class KaraToolRestrictionTests(unittest.TestCase):
    def _session(self, provider: _FakeProvider, allowed: set[str]):
        session = kara.KaraSession.__new__(kara.KaraSession)
        session.session_key = "scheduled:test"
        session.channel = "scheduled"
        session.model = "test-model"
        session.provider = provider
        session.messages = []
        session.session_path = Path("unused.md")
        session.allowed_tool_names = frozenset(allowed)
        session._persist = Mock()
        return session

    def test_chat_exposes_only_explicitly_allowed_tools(self) -> None:
        provider = _FakeProvider()
        session = self._session(provider, {"read_file", "web_search"})
        session._chat(with_tools=True)
        names = {item["function"]["name"] for item in provider.tool_batches[0]}
        self.assertEqual(names, {"read_file", "web_search"})

    def test_hallucinated_disallowed_tool_is_not_executed(self) -> None:
        provider = _FakeProvider(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": {"path": "x", "content": "bad"},
                                },
                            }
                        ],
                    }
                },
                {"message": {"role": "assistant", "content": "Handled safely."}},
            ]
        )
        session = self._session(provider, {"read_file"})
        write_mock = Mock(return_value="should not happen")
        with (
            patch.dict(kara.TOOL_REGISTRY, {"write_file": write_mock}),
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.memory_store, "log_turn"),
        ):
            reply = session.handle_message("Inspect only")
        self.assertEqual(reply, "Handled safely.")
        write_mock.assert_not_called()
        tool_messages = [m for m in session.messages if m.get("role") == "tool"]
        self.assertIn("not allowed", tool_messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
