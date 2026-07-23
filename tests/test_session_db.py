"""Regression tests for persisted tool-call correlation IDs."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import session_db


class TestToolCallPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = Path(self.tmp.name)
        self.patches = [
            patch.object(config, "BRAIN_DIR", self.brain),
            patch.object(session_db, "DB_PATH", self.brain / "state.db"),
        ]
        for item in self.patches:
            item.start()
        session_db._initialized = False

    def tearDown(self) -> None:
        session_db._initialized = False
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_backfill_assigns_legacy_tool_result_to_preceding_call(self) -> None:
        key = "kara:test:user:legacy"
        session_db.ensure_session(key, "test", "openai-codex", "gpt-5.6-terra")
        session_db.append_message(
            key,
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_legacy", "type": "function", "function": {"name": "search_memory", "arguments": "{}"}}
                ],
            },
        )
        session_db.append_message(
            key,
            {"role": "tool", "tool_name": "search_memory", "content": "legacy result"},
        )

        session_db.backfill_legacy_tool_call_ids()

        self.assertEqual(session_db.load_messages(key)[1]["tool_call_id"], "call_legacy")

    def test_load_messages_keeps_tool_call_id_for_responses_replay(self) -> None:
        key = "kara:test:user:1"
        session_db.ensure_session(key, "test", "openai-codex", "gpt-5.6-terra")
        session_db.append_message(
            key,
            {
                "role": "tool",
                "tool_name": "search_memory",
                "tool_call_id": "call_123",
                "content": "memory result",
            },
        )

        messages = session_db.load_messages(key)

        self.assertEqual(messages[0]["tool_call_id"], "call_123")
        self.assertEqual(messages[0]["tool_name"], "search_memory")


if __name__ == "__main__":
    unittest.main()
