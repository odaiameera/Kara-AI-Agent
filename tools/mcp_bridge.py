"""Generic stdio MCP client bridge for Kara's synchronous tool functions.

Kara's tool-calling loop (kara.py) is synchronous — every tool is a plain
Python function called from KaraSession.handle_message. The official MCP
Python SDK is async-only, so this module owns one background thread per MCP
server subprocess, running a persistent asyncio event loop, and exposes
``list_tools()``/``call_tool()`` as ordinary blocking calls. Any stdio MCP
server can be wired into Kara this way, not just Mnemosyne.

STUDY GUIDE
-----------
* One background thread per server keeps one long-lived MCP session alive,
  the same "shared singleton" pattern as tools/http_client.py's httpx.Client.
* ``asyncio.run_coroutine_threadsafe`` bridges a sync call into the
  background loop and blocks the caller for the result — the standard way
  to drive an async library from sync code without an event loop per call.
* Key concepts: background event loop thread, thread-safe coroutine
  submission, async context managers kept open across many calls.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

# LEARN: the `mcp` SDK is imported lazily inside _main(), not here. Importing it
# costs ~0.9-1.2s (it eagerly pulls in mcp.server.fastmcp + sse_starlette, which
# a stdio *client* never uses) — and that price would be paid on every gateway
# boot, every gateway self-restart, every CLI run, and every scheduled job, even
# when no MCP tool is ever called. Deferring it keeps `import kara` fast and
# matches the lazy-subprocess design below. Narrower import paths do NOT help;
# `mcp.client.session` still drags in the server modules.


class McpBridgeError(RuntimeError):
    """Raised when an MCP server subprocess can't be reached or returns an error."""


class McpServerBridge:
    """Owns one persistent background connection to one MCP server subprocess.

    The subprocess is started lazily on first use (not at import time), so
    Kara can start up fine even if the server's binary isn't installed yet —
    the error only surfaces when a tool that needs it is actually called.
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        connect_timeout: float = 20.0,
    ) -> None:
        self._command = command
        self._args = args
        self._env = env
        self._connect_timeout = connect_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any | None = None
        self._tools_cache: list[dict[str, Any]] = []
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._stop_event: asyncio.Event | None = None
        self._start_lock = threading.Lock()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()

    async def _main(self) -> None:
        self._stop_event = asyncio.Event()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            server_params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # LEARN: the server's tool list can't change while this
                    # subprocess lives, so cache it once here instead of paying a
                    # ~5ms round trip (24KB re-validated) on every tool call.
                    self._tools_cache = [
                        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                        for t in (await session.list_tools()).tools
                    ]
                    self._session = session
                    self._ready.set()
                    await self._stop_event.wait()
        except BaseException as exc:  # noqa: BLE001 - surfaced to callers, never raised in this thread
            self._start_error = exc
            self._ready.set()

    def ensure_started(self) -> None:
        """Start the background thread + subprocess on first call; no-op after that.

        Raises McpBridgeError if the server never became ready (missing
        binary, crashed on startup, handshake failure, etc).
        """
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run_loop, name=f"mcp-{self._command}", daemon=True
                )
                self._thread.start()
        if not self._ready.wait(self._connect_timeout):
            raise McpBridgeError(f"Timed out connecting to MCP server '{self._command}'.")
        if self._start_error is not None:
            raise McpBridgeError(f"Could not start MCP server '{self._command}': {self._start_error}")

    def _run_coro(self, coro_factory: Any) -> Any:
        # LEARN: coro_factory is a zero-arg callable, not an already-built coroutine.
        # Building the coroutine touches self._session (e.g. `self._session.list_tools()`),
        # so that must happen *after* ensure_started() guarantees it's set — building it
        # eagerly as an argument would evaluate `self._session.list_tools` (attribute
        # access on possibly-None) before the connection even starts.
        self.ensure_started()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        return future.result(self._connect_timeout)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP server's advertised tools (cached at connect time)."""
        self.ensure_started()
        return list(self._tools_cache)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one MCP tool by exact name and return the raw CallToolResult."""
        return self._run_coro(lambda: self._session.call_tool(name, arguments))  # type: ignore[union-attr]

    def stop(self) -> None:
        """Signal the background loop to close the session and exit its thread."""
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)


def extract_text(result: Any) -> str:
    """Flatten an MCP CallToolResult's text content blocks into one string."""
    content = getattr(result, "content", None) or []
    parts = [block.text for block in content if getattr(block, "type", "") == "text"]
    text = "\n".join(parts).strip()
    if getattr(result, "isError", False):
        return f"Tool error: {text or 'unknown error'}"
    return text or "(empty result)"
