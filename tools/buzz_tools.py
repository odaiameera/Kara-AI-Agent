"""Buzz CLI tools for Kara's ACP runtime.

Credentials are inherited from ``buzz-acp`` and are never accepted as tool
arguments. Commands use argv lists (no shell) and return only normalized JSON.
"""
from __future__ import annotations

import json
import os
import subprocess
from contextvars import ContextVar, Token
from dataclasses import dataclass

_REQUIRED_ENV = ("BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG")
_request_context: ContextVar["BuzzRequestContext | None"] = ContextVar(
    "kara_buzz_request", default=None
)


@dataclass
class BuzzRequestContext:
    channel: str
    reply_to: str
    published_event_id: str | None = None
    published: bool = False


def set_buzz_request_context(*, channel: str, reply_to: str) -> Token:
    """Bind outbound publication to one route supplied by the Buzz harness."""
    if not channel.strip() or not reply_to.strip():
        raise ValueError("Buzz channel and reply destination are required")
    return _request_context.set(BuzzRequestContext(channel.strip(), reply_to.strip()))


def reset_buzz_request_context(token: Token) -> None:
    _request_context.reset(token)


def buzz_message_was_published() -> bool:
    context = _request_context.get()
    return bool(context and context.published)


def buzz_send_message(content: str) -> str:
    """Publish a reply to the current authenticated Buzz conversation.

    Args:
        content: Message body to publish.
    """
    context = _request_context.get()
    if context is None:
        return json.dumps(
            {"ok": False, "error": "Buzz sends require a bound Buzz request context"}
        )
    if context.published:
        return json.dumps(
            {
                "ok": True,
                "accepted": True,
                "event_id": context.published_event_id,
                "duplicate_suppressed": True,
            }
        )
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        return json.dumps({"ok": False, "error": "missing runtime credentials", "missing": missing})
    if not content.strip():
        return json.dumps({"ok": False, "error": "content is required"})

    command = [
        "buzz", "messages", "send", "--channel", context.channel,
        "--content", content, "--reply-to", context.reply_to,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return json.dumps({"ok": False, "error": f"buzz CLI unavailable: {type(exc).__name__}"})

    if result.returncode != 0:
        # Normalize structured CLI errors and do not return arbitrary process
        # output, which could contain unexpected environment diagnostics.
        raw_error = (result.stderr or result.stdout).strip()
        try:
            error_payload = json.loads(raw_error)
            error = str(error_payload.get("message") or error_payload.get("error") or "buzz CLI failed")
        except json.JSONDecodeError:
            error = "buzz CLI failed without structured JSON"
        return json.dumps(
            {
                "ok": False,
                "exit_code": result.returncode,
                "error": error[:1000],
            }
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "error": "buzz CLI returned invalid JSON"})
    accepted = bool(payload.get("accepted"))
    event_id = payload.get("event_id")
    if accepted:
        context.published = True
        if isinstance(event_id, str) and event_id:
            context.published_event_id = event_id
    return json.dumps({"ok": accepted, "accepted": accepted, "event_id": event_id})
