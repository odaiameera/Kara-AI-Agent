from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.mcp_bridge import McpBridgeError, McpServerBridge, extract_text

_FAKE_SERVER_SOURCE = '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-test-server")


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the given text."""
    return f"echo: {text}"


@mcp.tool()
def boom() -> str:
    """Always raises to test error propagation."""
    raise RuntimeError("boom failure")


if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


class McpBridgeRealServerTests(unittest.TestCase):
    """Exercises the bridge against a real (throwaway) MCP server subprocess,
    not a mocked SDK, matching this repo's preference for real integration
    coverage over deep mocking (see tests/test_document_tools.py)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        script = Path(self._tmp.name) / "fake_mcp_server.py"
        script.write_text(_FAKE_SERVER_SOURCE, encoding="utf-8")
        self.bridge = McpServerBridge(sys.executable, [str(script)], connect_timeout=30.0)
        self.addCleanup(self.bridge.stop)

    def test_list_tools_returns_advertised_tools(self) -> None:
        tools = self.bridge.list_tools()
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"echo", "boom"})

    def test_call_tool_returns_text_result(self) -> None:
        result = self.bridge.call_tool("echo", {"text": "hello"})
        self.assertEqual(extract_text(result), "echo: hello")

    def test_call_tool_error_is_reported_not_raised(self) -> None:
        result = self.bridge.call_tool("boom", {})
        text = extract_text(result)
        self.assertIn("boom failure", text)

    def test_ensure_started_reuses_the_same_background_thread(self) -> None:
        self.bridge.list_tools()
        first_thread = self.bridge._thread
        self.bridge.list_tools()
        self.assertIs(self.bridge._thread, first_thread)


class McpBridgeFailureTests(unittest.TestCase):
    def test_missing_binary_raises_quickly(self) -> None:
        bridge = McpServerBridge("this-binary-does-not-exist-anywhere", ["mcp"], connect_timeout=10.0)
        self.addCleanup(bridge.stop)
        start = time.monotonic()
        with self.assertRaises(McpBridgeError):
            bridge.list_tools()
        self.assertLess(time.monotonic() - start, 10.0)

    def test_stop_is_safe_after_failed_startup_loop_has_closed(self) -> None:
        bridge = McpServerBridge("unused", [])
        loop = asyncio.new_event_loop()
        bridge._loop = loop
        bridge._stop_event = asyncio.Event()
        loop.close()

        bridge.stop()


class ExtractTextTests(unittest.TestCase):
    def test_extract_text_joins_multiple_text_blocks(self) -> None:
        class Block:
            def __init__(self, text: str) -> None:
                self.type = "text"
                self.text = text

        class Result:
            content = [Block("line one"), Block("line two")]
            isError = False

        self.assertEqual(extract_text(Result()), "line one\nline two")

    def test_extract_text_reports_empty_result(self) -> None:
        class Result:
            content: list = []
            isError = False

        self.assertEqual(extract_text(Result()), "(empty result)")


if __name__ == "__main__":
    unittest.main()
