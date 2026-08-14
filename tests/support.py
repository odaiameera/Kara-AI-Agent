"""Shared test helpers.

``KaraSession.__init__`` reaches the network (provider health check) and the
brain database, so tests build detached sessions via ``__new__`` instead. Doing
that inline in every test file meant each new session field broke several files
at once; this keeps the construction in one place.
"""
from __future__ import annotations

import threading
from typing import Any
from unittest.mock import Mock

import kara
from providers.base import ChatResult, ToolCall
from tools import registry


def make_session(
    provider: Any,
    *,
    session_key: str = "cli:test",
    channel: str = "cli",
    model: str = "test-model",
    allowed_tool_names: set[str] | frozenset[str] | None = None,
    active_groups: set[str] | None = None,
) -> kara.KaraSession:
    """Build a KaraSession with no network or database side effects."""
    session = kara.KaraSession.__new__(kara.KaraSession)
    session.session_key = session_key
    session.channel = channel
    session.model = model
    session.provider = provider
    session.messages = []
    session.allowed_tool_names = (
        None if allowed_tool_names is None else frozenset(allowed_tool_names)
    )
    session.active_groups = (
        set(registry.ALWAYS_ON) if active_groups is None else set(active_groups)
    )
    session._cancel = threading.Event()
    session._last_prompt_tokens = 0
    session._persist = Mock()
    return session


def tool_turn(name: str, arguments: dict, call_id: str = "call-1") -> ChatResult:
    """A ChatResult that asks for exactly one tool call."""
    return ChatResult(
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        finish_reason="tool_calls",
    )


class FakeProvider:
    """Returns queued ChatResults and records the tool schemas it was offered."""

    def __init__(self, responses: list[ChatResult] | None = None) -> None:
        self.responses = list(responses or [])
        self.tool_batches: list[list[dict] | None] = []
        self.message_batches: list[list[dict]] = []

    def chat(self, model, messages, tools=None):
        self.tool_batches.append(tools)
        self.message_batches.append([dict(m) for m in messages])
        if self.responses:
            return self.responses.pop(0)
        return ChatResult(content="ok")
