"""Mnemosyne memory MCP server tools for Kara.

Bridges Kara's synchronous tool-calling loop (via tools/mcp_bridge.py) to
Mnemosyne (https://github.com/mnemosyne-oss/mnemosyne) — a separate,
SQLite-backed, three-tier memory system for AI agents, run out-of-process as
`mnemosyne mcp` and spoken to over MCP. This sits *alongside* Kara's own
core/learnings/session memory (tools/memory_tools.py); it is not a
replacement, and the two systems are unaware of each other.

Mnemosyne documents its own MCP tool surface as version-specific, so beyond
the two well-known SDK functions (remember/recall) this module resolves
tool names against the server's live `list_tools()` at call time instead of
hardcoding them, plus exposes a generic escape hatch for everything else the
server offers (knowledge-graph, multi-agent, working-note, operational tools).

STUDY GUIDE
-----------
* Lazily creates one McpServerBridge on first use — never blocks Kara's
  startup if `mnemosyne` isn't installed.
* Fuzzy tool-name resolution instead of hardcoding, since the upstream docs
  explicitly call the tool inventory version-specific.
* Key concepts: lazy singleton, JSON argument passthrough, MCP error surfacing.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import config  # noqa: F401 - imported for its side effect: loads personal_agent/.env
from tools.mcp_bridge import McpBridgeError, McpServerBridge, extract_text

MNEMOSYNE_BIN = os.getenv("MNEMOSYNE_BIN", "mnemosyne").strip() or "mnemosyne"

# LEARN: Kara's .env holds Telegram/Ollama/Cloudflare/GitHub credentials. Handing
# os.environ wholesale to a third-party subprocess exports all of them, so strip
# secret-looking names (same denylist idiom as tools/python_tools.py) while
# always preserving MNEMOSYNE_* so Mnemosyne's own config still reaches it.
_SECRET_NAME_FRAGMENTS = ("api_key", "apikey", "token", "password", "secret", "credential", "cookie")

_bridge: McpServerBridge | None = None


def _venv_bin_candidates(name: str) -> list[Path]:
    # LEARN: sys.prefix reliably points at the active venv no matter how this
    # process was launched — unlike os.environ["PATH"], which does NOT include
    # .venv/Scripts when the gateway is started by directly invoking
    # .venv/Scripts/python.exe (Task Scheduler -> launch_gateway.vbs, or
    # gateway/restart.py's self-respawn), as opposed to `uv run`, which injects
    # it. shutil.which() alone works in a dev shell but silently fails for the
    # real running gateway, which is exactly the bug this fixes.
    subdir = "Scripts" if os.name == "nt" else "bin"
    venv_bin = Path(sys.prefix) / subdir
    if os.name == "nt":
        return [venv_bin / f"{name}.exe", venv_bin / name]
    return [venv_bin / name]


def _resolved_bin() -> str | None:
    configured = Path(MNEMOSYNE_BIN)
    if configured.exists():
        return str(configured)
    for candidate in _venv_bin_candidates(MNEMOSYNE_BIN):
        if candidate.exists():
            return str(candidate)
    return shutil.which(MNEMOSYNE_BIN)


def _not_ready_message() -> str:
    if _resolved_bin() is None:
        return (
            "Mnemosyne is not installed. From personal_agent/, run: "
            "uv add \"mnemosyne-memory[mcp,embeddings]\" "
            "(skip the [all] extra unless you have a C/C++ build toolchain — it pulls in "
            "llama-cpp-python, which compiles from source). "
            "Set MNEMOSYNE_BIN in .env only if the executable lives outside this venv."
        )
    return ""


def _subprocess_environment() -> dict[str, str]:
    """Copy the environment minus Kara's own secrets, keeping MNEMOSYNE_* intact."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper().startswith("MNEMOSYNE_")
        or not any(fragment in key.casefold() for fragment in _SECRET_NAME_FRAGMENTS)
    }


def _get_bridge() -> McpServerBridge:
    global _bridge
    if _bridge is None:
        _bridge = McpServerBridge(
            _resolved_bin() or MNEMOSYNE_BIN, ["mcp"], env=_subprocess_environment()
        )
    return _bridge


def _resolve_tool(*candidates: str) -> dict[str, Any]:
    """Find the live MCP tool (name + input schema) matching one of the given
    exact names, since Mnemosyne's own docs call the tool surface
    version-specific.

    A substring fallback covers an upstream rename, but only when it is
    unambiguous: 'remember' alone also matches ``mnemosyne_shared_remember``
    and ``mnemosyne_remember_canonical``, which write to entirely different
    memory tiers. Guessing between those would silently store the user's
    memory in the wrong place, so ambiguity raises instead.
    """
    by_name = {t["name"]: t for t in _get_bridge().list_tools()}
    for candidate in candidates:
        if candidate in by_name:
            return by_name[candidate]
    for candidate in candidates:
        matches = [tool for name, tool in by_name.items() if candidate in name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise McpBridgeError(
                f"'{candidate}' ambiguously matches {len(matches)} Mnemosyne tools "
                f"({', '.join(t['name'] for t in matches)}). Use mnemosyne_call_tool "
                "with the exact name you want."
            )
    raise McpBridgeError(
        f"None of the expected tool names ({', '.join(candidates)}) were found on the "
        f"Mnemosyne MCP server. Available tools: {', '.join(by_name) or '(none)'}"
    )


def _primary_text_field(schema: dict[str, Any] | None, properties: dict[str, Any]) -> str:
    """Pick the schema property that should carry the main text payload.

    Tries the known field names first (Mnemosyne 3.x uses ``content``; older
    shapes used ``text``), then falls back to the first required string
    property so an unfamiliar schema still works.
    """
    for name in ("content", "text"):
        if name in properties:
            return name
    for name in (schema or {}).get("required") or []:
        if (properties.get(name) or {}).get("type") == "string":
            return name
    return "content"


def mnemosyne_status() -> str:
    """
    Check whether Kara's separate Mnemosyne MCP memory server is installed and
    reachable, and list the tools it currently exposes.

    Returns:
        JSON with connection state and the live MCP tool inventory, or setup instructions.
    """
    err = _not_ready_message()
    if err:
        return err
    try:
        tools = _get_bridge().list_tools()
        return json.dumps(
            {
                "connected": True,
                "command": f"{_resolved_bin()} mcp",
                "tools": [{"name": t["name"], "description": t["description"]} for t in tools],
            },
            indent=2,
            ensure_ascii=False,
        )
    except McpBridgeError as exc:
        return f"Error connecting to Mnemosyne MCP server: {exc}"


def mnemosyne_remember(text: str, tags: str = "") -> str:
    """
    Store a memory in Mnemosyne, a separate long-term memory system reached over MCP.

    Args:
        text: The fact, note, or memory to store.
        tags: Optional comma-separated tags/labels.

    Returns:
        Confirmation text from Mnemosyne, or an error message.
    """
    err = _not_ready_message()
    if err:
        return err
    text = text.strip()
    if not text:
        return "Error: text is required."
    try:
        tool = _resolve_tool("remember", "store", "memory_store", "mnemosyne_remember")
        schema = tool["input_schema"]
        properties = (schema or {}).get("properties") or {}
        arguments: dict[str, Any] = {_primary_text_field(schema, properties): text}
        if tags.strip():
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if "tags" in properties:
                arguments["tags"] = tag_list
            elif "metadata" in properties:
                arguments["metadata"] = {"tags": tag_list}
        result = _get_bridge().call_tool(tool["name"], arguments)
        return extract_text(result)
    except McpBridgeError as exc:
        return f"Error storing memory in Mnemosyne: {exc}"


def mnemosyne_recall(query: str, limit: int = 10) -> str:
    """
    Search Mnemosyne, a separate long-term memory system reached over MCP, for relevant memories.

    Args:
        query: Natural-language search query.
        limit: Max results to return (1-50).

    Returns:
        Matching memories as text from Mnemosyne, or an error message.
    """
    err = _not_ready_message()
    if err:
        return err
    query = query.strip()
    if not query:
        return "Error: query is required."
    try:
        tool = _resolve_tool("recall", "search", "memory_recall", "mnemosyne_recall")
        limit_clamped = max(1, min(int(limit), 50))
        result = _get_bridge().call_tool(tool["name"], {"query": query, "limit": limit_clamped})
        return extract_text(result)
    except McpBridgeError as exc:
        return f"Error recalling from Mnemosyne: {exc}"


def mnemosyne_call_tool(tool_name: str, arguments_json: str = "{}") -> str:
    """
    Call any Mnemosyne MCP tool directly by its exact name — an escape hatch for
    knowledge-graph, multi-agent, working-note, and operational tools beyond
    remember/recall. Run mnemosyne_status first to see available tool names.

    Args:
        tool_name: Exact MCP tool name as reported by mnemosyne_status.
        arguments_json: JSON object string of arguments for that tool (default '{}').

    Returns:
        The tool's text result, or an error message.
    """
    err = _not_ready_message()
    if err:
        return err
    tool_name = tool_name.strip()
    if not tool_name:
        return "Error: tool_name is required."
    try:
        arguments = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as exc:
        return f"Error: arguments_json is not valid JSON: {exc}"
    if not isinstance(arguments, dict):
        return "Error: arguments_json must decode to a JSON object."
    try:
        result = _get_bridge().call_tool(tool_name, arguments)
        return extract_text(result)
    except McpBridgeError as exc:
        return f"Error calling Mnemosyne tool '{tool_name}': {exc}"
