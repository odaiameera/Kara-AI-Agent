"""Shared slash-command handlers for CLI and Telegram gateway.

STUDY GUIDE
-----------
* Parses /models, /model, /new, /restart and returns reply text or None.
* Same handlers work in CLI and Telegram — single source of truth for commands.
* Key concepts: early return pattern, string prefix checks, command dispatch.
"""
from __future__ import annotations

from datetime import datetime, timezone

import codex_auth
import config
import gateway.restart as gw_restart
import models
import session_db
from kara import KaraSession


def _format_usage(session: KaraSession) -> str:
    """Tokens and effort for this session and for today."""
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    here = session_db.usage_summary(session.session_key)
    daily = session_db.usage_summary(since=today)

    def block(title: str, data: dict) -> list[str]:
        seconds = data["duration_ms"] / 1000
        lines = [
            f"{title}:",
            f"  turns: {data['turns']}",
            f"  tokens in/out: {data['prompt_tokens']:,} / {data['completion_tokens']:,}"
            f"  (total {data['total_tokens']:,})",
            f"  tool calls: {data['tool_calls']} ({data['tool_errors']} failed)",
            f"  model time: {seconds:.1f}s",
        ]
        return lines

    lines = block("This session", here) + [""] + block("Today (all sessions)", daily)
    if not here["turns"] and not daily["turns"]:
        lines.append("")
        lines.append("No turns recorded yet.")
    return "\n".join(lines)


def _format_context(session: KaraSession) -> str:
    """How full the context window is, and when compaction will kick in."""
    used = session.context_tokens()
    window = config.MODEL_CONTEXT_TOKENS
    threshold = int(window * config.COMPACT_AT_FRACTION)
    percent = 100 * used / window if window else 0
    source = "provider-reported" if session._last_prompt_tokens else "estimated"

    return "\n".join(
        [
            f"Context: ~{used:,} / {window:,} tokens ({percent:.0f}%, {source})",
            f"  messages in history: {len(session.messages)}",
            f"  tool groups loaded: {', '.join(sorted(session.active_groups))}",
            f"  compaction starts at: {threshold:,} tokens",
            f"  model: {session.provider.id} / {session.model}",
        ]
    )


def handle_command(session: KaraSession, text: str) -> str | None:
    """Return response text if *text* is a slash command, else None."""
    stripped = text.strip()
    lower = stripped.lower()

    # LEARN: Return str for commands, None for normal chat — caller uses None to mean "not a command".
    if lower == "/models":
        return models.format_models_list()
    if lower == "/providers":
        return models.format_providers_list()
    if lower in {"/auth", "/auth codex", "/login codex"}:
        if lower == "/auth":
            return "Auth commands: /auth codex"
        return (
            "OpenAI Codex login is done from a local terminal so you can see the browser code:\n"
            "  uv run python codex_auth.py login\n\n"
            "Then restart Kara or send /restart, and use /codex-status to verify."
        )
    if lower == "/codex-status":
        try:
            codex_auth.read_tokens()
            return f"OpenAI Codex credentials are stored at {codex_auth.auth_file()}."
        except Exception as e:
            return str(e)
    if lower == "/model":
        return models.format_model_list()
    if lower.startswith("/model "):
        try:
            return session.switch_model(stripped[len("/model ") :].strip())
        except Exception as e:
            return f"Could not switch model: {e}"
    if lower == "/provider":
        return models.format_providers_list()
    if lower.startswith("/provider "):
        parts = stripped[len("/provider ") :].strip().split(maxsplit=1)
        provider_id = parts[0] if parts else ""
        model = parts[1] if len(parts) > 1 else None
        try:
            return session.switch_provider(provider_id, model)
        except Exception as e:
            return f"Could not switch provider: {e}"
    if lower == "/usage":
        return _format_usage(session)
    if lower == "/context":
        return _format_context(session)
    if lower == "/stop":
        # On Telegram /stop is handled by its own handler so it can reach a turn
        # that is still running. Reaching it here means nothing was in flight.
        return "Nothing is running."
    if lower == "/new":
        return session.reset_conversation()
    if lower == "/restart":
        gw_restart.request_restart("user command")
        return "Gateway restart scheduled. I'll be back in a few seconds."
    return None
