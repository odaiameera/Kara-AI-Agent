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
from dataclasses import dataclass, field
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


# --- Compaction ----------------------------------------------------------------
# History grows without bound: every turn appends a user message, an assistant
# message, and one tool message per tool call, and nothing ever removed them. A
# long-running session eventually exceeds any window, and before this there was
# no recovery from that except starting a new conversation.

TRIMMED_MARKER = "\n... (trimmed from {original} chars by context compaction)"
LIVE_CAP_MARKER = (
    "\n... (truncated from {original} chars: a single tool result may not exceed "
    "{share}% of the context window)"
)
# Share of the window one tool result may occupy on the turn that requested it.
LIVE_RESULT_SHARE = 0.25


def live_tool_result_cap(window_tokens: int | None = None) -> int:
    """Character cap for a tool result entering the current turn.

    Between-turn compaction cannot help the turn that is running: a result
    arrives after compaction and goes straight into the next request. Tools cap
    their own output, but generously enough (up to 50,000 characters) that one
    result could still overrun a small window on its own.
    """
    window = window_tokens or config.MODEL_CONTEXT_TOKENS
    return int(window * CHARS_PER_TOKEN * LIVE_RESULT_SHARE)


def cap_tool_result(content: str, *, window_tokens: int | None = None) -> str:
    cap = live_tool_result_cap(window_tokens)
    if len(content) <= cap:
        return content
    return content[:cap] + LIVE_CAP_MARKER.format(
        original=len(content), share=int(LIVE_RESULT_SHARE * 100)
    )


@dataclass
class CompactionReport:
    """What compaction actually did, for logging and tests."""

    trimmed_results: int = 0
    dropped_units: int = 0
    dropped_messages: int = 0
    tokens_before: int = 0
    tokens_after: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.trimmed_results or self.dropped_units)


@dataclass
class _Unit:
    """An atomic group of messages that must be kept or dropped together.

    An assistant message carrying ``tool_calls`` and its matching ``tool``
    results are one unit. Splitting them breaks the call/result correlation that
    the Codex Responses replay depends on — the reason
    ``session_db._backfill_legacy_tool_call_ids`` had to exist.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return estimate_messages_tokens(self.messages)

    @property
    def user_text(self) -> str:
        for message in self.messages:
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @property
    def tool_names(self) -> list[str]:
        return [
            str(m.get("tool_name") or "")
            for m in self.messages
            if m.get("role") == "tool" and m.get("tool_name")
        ]


NOTE_PREFIX = "[earlier conversation compacted"
_MAX_REMEMBERED_ASKS = 12


def is_compaction_note(message: dict[str, Any]) -> bool:
    return message.get("role") == "system" and str(
        message.get("content") or ""
    ).startswith(NOTE_PREFIX)


def _split_system_prefix(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    """Separate leading system messages, any prior compaction note, and the rest.

    The note is pulled out rather than treated as part of the prefix: left in, a
    fresh note would be appended beside it on every compaction and never be
    droppable, so a long session accumulated one permanent note per compaction.
    """
    index = 0
    prefix: list[dict[str, Any]] = []
    note: dict[str, Any] | None = None
    while index < len(messages) and messages[index].get("role") == "system":
        message = messages[index]
        if is_compaction_note(message):
            note = message
        else:
            prefix.append(message)
        index += 1
    return prefix, note, messages[index:]


def _parse_note(note: dict[str, Any] | None) -> tuple[int, list[str], list[str]]:
    """Recover (dropped count, asks, tools) from a previous note so it can merge."""
    if note is None:
        return 0, [], []
    dropped = 0
    asks: list[str] = []
    tools: list[str] = []
    for line in str(note.get("content") or "").splitlines():
        line = line.strip()
        if line.startswith(NOTE_PREFIX):
            digits = "".join(ch for ch in line if ch.isdigit())
            dropped = int(digits) if digits else 0
        elif line.startswith("- "):
            asks.append(line[2:])
        elif line.startswith("Tools used earlier:"):
            tools = [
                name.strip().rstrip(".")
                for name in line[len("Tools used earlier:") :].split(",")
                if name.strip()
            ]
    return dropped, asks, tools


def group_units(messages: list[dict[str, Any]]) -> list[_Unit]:
    """Group messages so tool calls stay attached to their results."""
    units: list[_Unit] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        unit = _Unit([message])
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            # Absorb every tool result that answers this assistant turn.
            while index < len(messages) and messages[index].get("role") == "tool":
                unit.messages.append(messages[index])
                index += 1
        units.append(unit)
    return units


def _trim_tool_results(unit: _Unit, max_chars: int) -> int:
    trimmed = 0
    for message in unit.messages:
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if len(content) <= max_chars:
            continue
        message["content"] = content[:max_chars] + TRIMMED_MARKER.format(
            original=len(content)
        )
        trimmed += 1
    return trimmed


def _summary_note(
    units: list[_Unit], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the single note describing everything dropped so far.

    Merges any previous note rather than sitting beside it, so repeated
    compaction of a long session keeps exactly one note of bounded size.
    """
    prior_dropped, asks, tools = _parse_note(previous)

    for unit in units:
        text = unit.user_text.strip().replace("\n", " ")
        if text:
            asks.append(text[:120])
        tools.extend(unit.tool_names)

    total = prior_dropped + len(units)
    asks = asks[-_MAX_REMEMBERED_ASKS:]

    lines = [f"{NOTE_PREFIX}: {total} exchanges removed]"]
    if asks:
        lines.append("Earlier you asked about:")
        lines.extend(f"- {ask}" for ask in asks)
    if tools:
        lines.append(f"Tools used earlier: {', '.join(sorted(set(tools))[:15])}.")
    lines.append(
        "The full transcript is still in Kara's session history; use search_memory "
        "if you need detail from before this point."
    )
    return {"role": "system", "content": "\n".join(lines)}


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    limit_tokens: int | None = None,
    max_tool_result_chars: int | None = None,
    keep_recent_units: int = 4,
) -> tuple[list[dict[str, Any]], CompactionReport]:
    """Bring ``messages`` under budget, preserving tool-call correlation.

    Two levers, cheapest first: trim oversized tool results, then drop whole
    oldest units into a summary note. Leading system messages and the most recent
    ``keep_recent_units`` units are never dropped.

    Trimming applies to every unit, not just old ones. This runs between turns —
    ``KaraSession.compact_if_needed`` calls it before the new user message is
    added — so no tool result is still in flight; each was already delivered in
    full to the turn that asked for it. Protecting recent units from trimming
    meant a couple of large document reads could keep the history over budget
    with nothing left to drop.

    Returns a new list; the input is not mutated.
    """
    limit = limit_tokens or int(config.MODEL_CONTEXT_TOKENS * config.COMPACT_AT_FRACTION)
    max_chars = max_tool_result_chars or config.MAX_TOOL_RESULT_CHARS

    working = [dict(message) for message in messages]
    report = CompactionReport(tokens_before=estimate_messages_tokens(working))
    if report.tokens_before <= limit:
        report.tokens_after = report.tokens_before
        return working, report

    system_prefix, previous_note, rest = _split_system_prefix(working)
    units = group_units(rest)

    # Lever 1 — trim oversized tool results anywhere in the retained history.
    for unit in units:
        report.trimmed_results += _trim_tool_results(unit, max_chars)

    note_tokens = estimate_message_tokens(previous_note) if previous_note else 0

    def current_tokens() -> int:
        return (
            estimate_messages_tokens(system_prefix)
            + note_tokens
            + sum(u.tokens for u in units)
        )

    # Lever 2 — drop the oldest units whole, keeping a summary of what they were.
    dropped: list[_Unit] = []
    while current_tokens() > limit and len(units) > keep_recent_units:
        dropped.append(units.pop(0))

    rebuilt = list(system_prefix)
    if dropped:
        report.dropped_units = len(dropped)
        report.dropped_messages = sum(len(u.messages) for u in dropped)
        rebuilt.append(_summary_note(dropped, previous_note))
    elif previous_note is not None:
        rebuilt.append(previous_note)
    for unit in units:
        rebuilt.extend(unit.messages)

    report.tokens_after = estimate_messages_tokens(rebuilt)
    return rebuilt, report
