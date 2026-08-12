"""Token accounting for Kara's context window.

Deliberately dependency-free and side-effect-free: callers pass in the text and
schemas they already have, so this module never imports ``kara`` and cannot
create an import cycle.

Estimates are approximate. Real prompt token counts arrive on every provider
response (``ChatResult.usage.prompt_tokens``) and should be preferred wherever
they are available; the heuristic here covers the first request of a session and
anything the provider does not report.
"""
from __future__ import annotations

import json
from typing import Any

import config

# Rough English + JSON average. Schemas are punctuation-dense and run somewhat
# hotter than prose, so this under-counts slightly — which is the safe direction
# for a budget check.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_schema_tokens(schemas: list[dict[str, Any]] | None) -> int:
    if not schemas:
        return 0
    return estimate_tokens(json.dumps(schemas))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Approximate tokens for one chat message, including its tool calls."""
    total = estimate_tokens(str(message.get("content") or ""))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += estimate_tokens(json.dumps(tool_calls))
    # Role, separators and message framing cost a handful of tokens each.
    return total + 4


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def fixed_overhead_tokens(system_prompt: str, schemas: list[dict[str, Any]] | None) -> int:
    """Tokens spent before the conversation contributes anything."""
    return estimate_tokens(system_prompt) + estimate_schema_tokens(schemas)


def check_configured_window(
    system_prompt: str,
    schemas: list[dict[str, Any]] | None,
    *,
    window: int | None = None,
) -> str | None:
    """Return a warning if the configured window cannot comfortably hold a turn.

    Ollama truncates silently rather than erroring, so a window smaller than the
    fixed overhead would quietly discard the head of the prompt — the system
    prompt and every safety rule in it. Surfacing this at startup turns a silent
    failure into a visible one.
    """
    limit = window or config.MODEL_CONTEXT_TOKENS
    overhead = fixed_overhead_tokens(system_prompt, schemas)
    if overhead >= limit:
        return (
            f"Context window ({limit} tokens) is smaller than Kara's fixed overhead "
            f"(~{overhead} tokens of system prompt + tool schemas). The model will "
            f"silently truncate the prompt. Raise KARA_MODEL_CONTEXT_TOKENS."
        )
    if overhead > limit // 2:
        return (
            f"Fixed overhead (~{overhead} tokens) uses {100 * overhead // limit}% of the "
            f"context window ({limit} tokens), leaving room for only ~{limit - overhead} "
            f"tokens of conversation. Consider raising KARA_MODEL_CONTEXT_TOKENS."
        )
    return None
