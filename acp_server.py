"""Minimal Agent Client Protocol stdio adapter for Kara.

Buzz's ``buzz-acp`` harness owns relay subscriptions and supplies each event as
an ACP prompt. This module keeps JSON-RPC on stdout and sends diagnostics to
stderr so the wire protocol cannot be corrupted by normal logging.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from typing import Any, TextIO

from tools.buzz_tools import (
    buzz_message_was_published,
    buzz_send_message,
    reset_buzz_request_context,
    set_buzz_request_context,
)

PROTOCOL_VERSION = 1
_CONTEXT_START = re.compile(r"(?m)^\[Context\]$")
_SECTION_HEADER = re.compile(r"(?m)^\[(?!Context\]$)[^\r\n]+\]")
_USER_CONTENT_START = re.compile(
    r"(?m)^\[(?:Thread Context|New message|What you were working on)[^\r\n]*\]$"
)
_CHANNEL_ROUTE = re.compile(
    r"(?m)^Channel: .* \(#([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)$"
)
_THREAD_ROOT = re.compile(r"(?m)^Thread root: ([0-9a-fA-F]{64})$")
_REPLY_DESTINATION = re.compile(r"--reply-to ([0-9a-fA-F]{64})(?:\s|`|$)")


def _buzz_route(prompt: str) -> tuple[str, str]:
    """Extract the last valid harness route before user-controlled content."""
    user_content = _USER_CONTENT_START.search(prompt)
    trusted_prefix = prompt[: user_content.start()] if user_content else prompt
    routes: list[tuple[str, str]] = []
    for context_start in _CONTEXT_START.finditer(trusted_prefix):
        context = trusted_prefix[context_start.start() :]
        next_section = _SECTION_HEADER.search(context, len(context_start.group(0)))
        if next_section:
            context = context[: next_section.start()]

        channel_match = _CHANNEL_ROUTE.search(context)
        thread_match = _THREAD_ROOT.search(context)
        reply_match = _REPLY_DESTINATION.search(context)
        route_match = thread_match if thread_match is not None else reply_match
        if channel_match and route_match:
            routes.append((channel_match.group(1), route_match.group(1)))

    if not routes:
        raise ValueError("prompt does not contain a trusted Buzz route")
    return routes[-1]


def _create_kara_session(session_key: str):
    # Import lazily so protocol framing can be tested and `--help`-style
    # diagnostics can run before Kara's optional providers are loaded.
    from kara import KaraSession

    return KaraSession(session_key, channel="buzz")


class KaraACPServer:
    """Serve the ACP methods required by ``buzz-acp`` over newline JSON-RPC."""

    def __init__(self, output: TextIO = sys.stdout):
        self.output = output
        self.sessions: dict[str, Any] = {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.output.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.output.flush()

    def _result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _update(self, session_id: str, text: str) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                },
            }
        )

    @staticmethod
    def _prompt_text(blocks: Any) -> str:
        if not isinstance(blocks, list):
            raise ValueError("prompt must be an array")
        texts = [
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(part for part in texts if isinstance(part, str)).strip()
        if not text:
            raise ValueError("Kara currently accepts text ACP prompts only")
        return text

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        try:
            if method == "initialize":
                requested = params.get("protocolVersion", PROTOCOL_VERSION)
                self._result(
                    request_id,
                    {
                        "protocolVersion": requested,
                        "agentInfo": {
                            "name": "kara-acp",
                            "title": "Kara",
                            "version": "0.1.0",
                        },
                        "agentCapabilities": {
                            "loadSession": False,
                            "promptCapabilities": {
                                "image": False,
                                "audio": False,
                                "embeddedContext": False,
                            },
                            "mcpCapabilities": {
                                "http": False,
                                "sse": False,
                                "acp": False,
                            },
                        },
                        "authMethods": [],
                    },
                )
                return

            if method == "session/new":
                session_id = uuid.uuid4().hex
                # Defer provider/config loading until the first real prompt.
                # This keeps ACP discovery and health checks independent of
                # model reachability while preserving one KaraSession per ACP session.
                self.sessions[session_id] = None
                self._result(request_id, {"sessionId": session_id})
                return

            if method == "session/prompt":
                session_id = str(params.get("sessionId") or "")
                if session_id not in self.sessions:
                    self._error(request_id, -32602, "unknown sessionId")
                    return
                session = self.sessions[session_id]
                if session is None:
                    session = _create_kara_session(f"kara:acp:{session_id}")
                    self.sessions[session_id] = session
                prompt = self._prompt_text(params.get("prompt"))
                channel, reply_to = _buzz_route(prompt)
                context_token = set_buzz_request_context(
                    channel=channel, reply_to=reply_to
                )
                try:
                    reply = session.handle_message(prompt)
                    if not buzz_message_was_published():
                        publish_result = json.loads(buzz_send_message(reply))
                        if not publish_result.get("ok"):
                            raise RuntimeError(
                                "Buzz final-response publish failed: "
                                + str(publish_result.get("error") or "unknown error")
                            )
                finally:
                    reset_buzz_request_context(context_token)
                self._update(session_id, reply)
                self._result(request_id, {"stopReason": "end_turn"})
                return

            if method == "session/cancel":
                # Kara's provider calls are synchronous today. A cancellation is
                # accepted as a notification; the harness may terminate the ACP
                # subprocess if it needs immediate interruption.
                return

            if request_id is not None:
                self._error(request_id, -32601, f"method not found: {method}")
        except ValueError as exc:
            self._error(request_id, -32602, str(exc))
        except Exception as exc:
            print(f"kara-acp request failed: {exc}", file=sys.stderr, flush=True)
            self._error(request_id, -32603, "Kara runtime request failed")


def serve(input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout) -> None:
    server = KaraACPServer(output_stream)
    for line in input_stream:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            server._error(None, -32700, "parse error")
            continue
        if not isinstance(message, dict):
            server._error(None, -32600, "invalid request")
            continue
        server.handle(message)


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
