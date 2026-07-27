from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import mnemosyne_tools
from tools.mcp_bridge import McpBridgeError


def _fake_call_result(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    result = MagicMock()
    result.content = [block]
    result.isError = False
    return result


class NotReadyTests(unittest.TestCase):
    def test_not_ready_message_when_binary_missing(self) -> None:
        with patch.object(mnemosyne_tools, "_resolved_bin", return_value=None):
            self.assertIn("not installed", mnemosyne_tools._not_ready_message())

    def test_not_ready_message_empty_when_binary_found(self) -> None:
        with patch.object(mnemosyne_tools, "_resolved_bin", return_value="C:/bin/mnemosyne.exe"):
            self.assertEqual(mnemosyne_tools._not_ready_message(), "")

    def test_status_reports_setup_instructions_when_not_installed(self) -> None:
        with patch.object(mnemosyne_tools, "_resolved_bin", return_value=None):
            result = mnemosyne_tools.mnemosyne_status()
        self.assertIn("uv add", result)


class BinResolutionTests(unittest.TestCase):
    """Regression coverage for the real bug found when the gateway (launched by
    directly invoking .venv/Scripts/python.exe via Task Scheduler) couldn't see
    Mnemosyne even though `uv run` could: shutil.which() depends on PATH, which
    does NOT include .venv/Scripts unless something explicitly activated the
    venv. sys.prefix does not have that problem."""

    def test_finds_executable_colocated_with_the_running_interpreter_even_without_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_prefix = Path(raw)
            scripts_dir = fake_prefix / "Scripts"
            scripts_dir.mkdir()
            exe = scripts_dir / "mnemosyne.exe"
            exe.write_bytes(b"")

            with patch.object(mnemosyne_tools.sys, "prefix", str(fake_prefix)), patch.object(
                mnemosyne_tools.os, "name", "nt"
            ), patch.object(mnemosyne_tools.shutil, "which", return_value=None):
                resolved = mnemosyne_tools._resolved_bin()

        self.assertEqual(resolved, str(exe))

    def test_falls_back_to_path_search_when_not_colocated_with_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            empty_prefix = Path(raw)
            with patch.object(mnemosyne_tools.sys, "prefix", str(empty_prefix)), patch.object(
                mnemosyne_tools.shutil, "which", return_value="C:/somewhere/on/path/mnemosyne.exe"
            ):
                resolved = mnemosyne_tools._resolved_bin()

        self.assertEqual(resolved, "C:/somewhere/on/path/mnemosyne.exe")


class StatusAndToolCallTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(mnemosyne_tools, "_resolved_bin", return_value="mnemosyne")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = MagicMock()
        bridge_patcher = patch.object(mnemosyne_tools, "_get_bridge", return_value=self.bridge)
        bridge_patcher.start()
        self.addCleanup(bridge_patcher.stop)

    def test_status_lists_live_tool_inventory(self) -> None:
        self.bridge.list_tools.return_value = [
            {"name": "remember", "description": "Store a memory", "input_schema": {}},
            {"name": "recall", "description": "Search memories", "input_schema": {}},
        ]
        result = json.loads(mnemosyne_tools.mnemosyne_status())
        self.assertTrue(result["connected"])
        self.assertEqual({t["name"] for t in result["tools"]}, {"remember", "recall"})

    def test_status_surfaces_bridge_connection_errors(self) -> None:
        self.bridge.list_tools.side_effect = McpBridgeError("boom")
        result = mnemosyne_tools.mnemosyne_status()
        self.assertIn("Error connecting", result)

    def test_remember_maps_text_to_content_and_tags_into_metadata(self) -> None:
        # Matches the real installed mnemosyne-memory==3.14.0 schema: the payload
        # field is "content" (not "text"), and there's no top-level "tags" — it
        # nests under "metadata". Verified against the live MCP server before
        # writing this test (see conversation: initial "text" mapping 400'd with
        # "'content' is a required property").
        self.bridge.list_tools.return_value = [
            {
                "name": "mnemosyne_remember",
                "description": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["content"],
                },
            }
        ]
        self.bridge.call_tool.return_value = _fake_call_result("stored ok")

        result = mnemosyne_tools.mnemosyne_remember("User likes dark mode", tags="preferences, ui")

        self.assertEqual(result, "stored ok")
        self.bridge.call_tool.assert_called_once_with(
            "mnemosyne_remember",
            {"content": "User likes dark mode", "metadata": {"tags": ["preferences", "ui"]}},
        )

    def test_remember_uses_top_level_tags_field_when_schema_offers_one(self) -> None:
        self.bridge.list_tools.return_value = [
            {
                "name": "remember",
                "description": "",
                "input_schema": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}, "tags": {"type": "array"}},
                    "required": ["content"],
                },
            }
        ]
        self.bridge.call_tool.return_value = _fake_call_result("stored ok")

        mnemosyne_tools.mnemosyne_remember("A fact", tags="x, y")

        self.bridge.call_tool.assert_called_once_with("remember", {"content": "A fact", "tags": ["x", "y"]})

    def test_remember_falls_back_to_text_field_when_no_content_field(self) -> None:
        self.bridge.list_tools.return_value = [
            {
                "name": "remember",
                "description": "",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]
        self.bridge.call_tool.return_value = _fake_call_result("stored ok")

        mnemosyne_tools.mnemosyne_remember("A fact")

        self.bridge.call_tool.assert_called_once_with("remember", {"text": "A fact"})

    def test_remember_falls_back_to_required_string_property_when_schema_is_unfamiliar(self) -> None:
        self.bridge.list_tools.return_value = [
            {
                "name": "remember",
                "description": "",
                "input_schema": {
                    "type": "object",
                    "properties": {"memo": {"type": "string"}},
                    "required": ["memo"],
                },
            }
        ]
        self.bridge.call_tool.return_value = _fake_call_result("stored ok")

        mnemosyne_tools.mnemosyne_remember("A fact")

        self.bridge.call_tool.assert_called_once_with("remember", {"memo": "A fact"})

    def test_remember_resolves_fuzzy_tool_name_when_exact_name_differs(self) -> None:
        # Mimics a versioned server exposing e.g. "mnemosyne_remember" instead of "remember".
        self.bridge.list_tools.return_value = [
            {
                "name": "mnemosyne_remember",
                "description": "",
                "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}},
            }
        ]
        self.bridge.call_tool.return_value = _fake_call_result("stored ok")

        result = mnemosyne_tools.mnemosyne_remember("A fact")

        self.assertEqual(result, "stored ok")
        self.bridge.call_tool.assert_called_once_with("mnemosyne_remember", {"content": "A fact"})

    def test_remember_requires_non_empty_text(self) -> None:
        result = mnemosyne_tools.mnemosyne_remember("   ")
        self.assertIn("Error", result)
        self.bridge.call_tool.assert_not_called()

    def test_recall_clamps_limit_and_resolves_tool_name(self) -> None:
        self.bridge.list_tools.return_value = [{"name": "recall", "description": "", "input_schema": {}}]
        self.bridge.call_tool.return_value = _fake_call_result("no results")

        mnemosyne_tools.mnemosyne_recall("user preferences", limit=999)

        self.bridge.call_tool.assert_called_once_with("recall", {"query": "user preferences", "limit": 50})

    def test_recall_reports_missing_tool_name(self) -> None:
        self.bridge.list_tools.return_value = [{"name": "unrelated_tool", "description": "", "input_schema": {}}]

        result = mnemosyne_tools.mnemosyne_recall("anything")

        self.assertIn("Error recalling", result)
        self.assertIn("unrelated_tool", result)

    def test_call_tool_rejects_invalid_json_arguments(self) -> None:
        result = mnemosyne_tools.mnemosyne_call_tool("some_tool", "{not json")
        self.assertIn("not valid JSON", result)
        self.bridge.call_tool.assert_not_called()

    def test_call_tool_rejects_non_object_arguments(self) -> None:
        result = mnemosyne_tools.mnemosyne_call_tool("some_tool", "[1, 2, 3]")
        self.assertIn("JSON object", result)
        self.bridge.call_tool.assert_not_called()

    def test_call_tool_passes_through_exact_name_and_arguments(self) -> None:
        self.bridge.call_tool.return_value = _fake_call_result("done")

        result = mnemosyne_tools.mnemosyne_call_tool("knowledge_graph_query", '{"node": "odai"}')

        self.assertEqual(result, "done")
        self.bridge.call_tool.assert_called_once_with("knowledge_graph_query", {"node": "odai"})


if __name__ == "__main__":
    unittest.main()
