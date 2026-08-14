"""Single source of truth for Kara's tool surface.

Every module in ``tools/`` declares its own ``TOOL_GROUP``, ``TOOLS`` and
``SCHEDULED_SAFE``. This module aggregates those declarations into the registry,
the JSON schemas, and the scheduled-run allowlist, so adding a tool means
editing exactly one file.

Groups are also the unit of *gating*: an interactive session starts with only
the always-on groups exposed to the model and activates the rest on demand.
That keeps a typical request from carrying all 84 tool schemas (~40KB) when it
needs four of them.

Gating is a presentation filter only. ``KaraSession.allowed_tool_names`` remains
the execution boundary and is never widened by anything here.
"""
from __future__ import annotations

import importlib
import re
from typing import Any, Callable

import tool_schemas

# Explicit, not a package walk: an import error here should be loud, and the
# set of tool modules should stay greppable.
MODULE_NAMES = (
    "memory_tools",
    "web_tools",
    "file_tools",
    "document_tools",
    "office_tools",
    "sql_tools",
    "python_tools",
    "computer_tools",
    "windows_tools",
    "scheduler_tools",
    "email_tools",
    "github_tools",
    "mnemosyne_tools",
    "obsidian_tools",
)

# Groups exposed on every request. Everything else is activated on demand.
ALWAYS_ON = frozenset({"memory", "web", "file", "document"})

# Lowercase substrings that pre-activate a group from the user's message. Matched
# on word boundaries, so "pr" does not fire on "prefer".
GROUP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "github": ("github", "repo", "repos", "repository", "pull request", "pull requests",
               "pr", "prs", "issue", "issues", "commit", "commits", "branch", "branches",
               "clone", "git", "merge", "workflow", "actions"),
    "email": ("email", "emails", "mail", "inbox", "mailbox", "unread", "imap", "himalaya"),
    "office": ("word", "excel", "powerpoint", "docx", "xlsx", "pptx", "spreadsheet",
               "workbook", "slide", "slides", "presentation", "deck"),
    "computer": ("desktop", "click", "keyboard", "window", "screen", "gui", "app",
                 "accessibility", "computer use"),
    "windows": ("process", "processes", "service", "services", "disk", "cpu", "ram",
                "task manager", "uptime", "system"),
    "sql": ("sqlite", "database", "db", "sql", "table", "tables", "schema"),
    "python": ("python", "py", "unittest", "pytest", "test suite", "tests"),
    "scheduler": ("remind", "reminder", "reminders", "schedule", "scheduled", "cron",
                  "recurring", "every day", "every morning", "daily", "weekly", "later"),
    "mnemosyne": ("mnemosyne",),
    "obsidian": ("obsidian", "vault"),
}


def _load() -> list[Any]:
    return [importlib.import_module(f"tools.{name}") for name in MODULE_NAMES]


_MODULES = _load()

ALL_TOOLS: list[Callable[..., Any]] = []
GROUPS: dict[str, tuple[str, ...]] = {}
_GROUP_OF: dict[str, str] = {}
_SCHEDULED_SAFE: set[str] = set()
_READ_ONLY: set[str] = set()

for _module in _MODULES:
    _group = _module.TOOL_GROUP
    _names: list[str] = []
    for _fn in _module.TOOLS:
        if _fn.__name__ in _GROUP_OF:
            raise RuntimeError(
                f"Duplicate tool name '{_fn.__name__}' in group '{_group}' "
                f"(already registered by '{_GROUP_OF[_fn.__name__]}')."
            )
        ALL_TOOLS.append(_fn)
        _names.append(_fn.__name__)
        _GROUP_OF[_fn.__name__] = _group
    GROUPS[_group] = tuple(_names)

    for _attr, _sink in (
        ("SCHEDULED_SAFE", _SCHEDULED_SAFE),
        ("READ_ONLY", _READ_ONLY),
    ):
        _declared = set(getattr(_module, _attr))
        _unknown = _declared - set(_names)
        if _unknown:
            raise RuntimeError(
                f"{_module.__name__}.{_attr} names tools it does not define: "
                f"{sorted(_unknown)}"
            )
        _sink.update(_declared)

    # A tool cannot be safe to run unattended without also being side-effect free.
    _unsafe = set(_module.SCHEDULED_SAFE) - set(_module.READ_ONLY)
    if _unsafe:
        raise RuntimeError(
            f"{_module.__name__}.SCHEDULED_SAFE contains tools missing from "
            f"READ_ONLY: {sorted(_unsafe)}"
        )

# Group names are declared by the tool modules (``TOOL_GROUP``) and referenced
# again by ALWAYS_ON and GROUP_KEYWORDS. Nothing tied those copies together, and
# a name that matches no real group fails silently rather than loudly: an unknown
# ALWAYS_ON entry contributes no schemas, and an unknown GROUP_KEYWORDS key is
# dropped by the ``group in GROUPS`` filter below. Either way the group simply
# stops appearing, with no error and no failing test.
for _label, _referenced in (
    ("ALWAYS_ON", set(ALWAYS_ON)),
    ("GROUP_KEYWORDS", set(GROUP_KEYWORDS)),
):
    _unknown = _referenced - set(GROUPS)
    if _unknown:
        raise RuntimeError(
            f"registry.{_label} names groups no tool module declares: "
            f"{sorted(_unknown)}. Known groups: {sorted(GROUPS)}."
        )

TOOL_REGISTRY: dict[str, Callable[..., Any]] = {fn.__name__: fn for fn in ALL_TOOLS}
TOOL_SCHEMAS: list[dict[str, Any]] = tool_schemas.build_tools(ALL_TOOLS)
SCHEMAS_BY_NAME: dict[str, dict[str, Any]] = {
    item["function"]["name"]: item for item in TOOL_SCHEMAS
}
SCHEDULED_SAFE = frozenset(_SCHEDULED_SAFE)
READ_ONLY = frozenset(_READ_ONLY)
ON_DEMAND_GROUPS = frozenset(GROUPS) - ALWAYS_ON
# Same set, in registry order, for anything user- or model-facing that needs a
# stable list rather than a set's arbitrary iteration order.
ON_DEMAND_GROUP_ORDER = tuple(group for group in GROUPS if group not in ALWAYS_ON)

ACTIVATE_TOOL = "activate_tool_group"


def activate_tool_group(group: str) -> str:
    """
    Reveal a group of tools that is not currently loaded, then retry what you were doing. Use this when you need a capability whose tools you cannot see.

    Args:
        group: The capability group to load. One of: {groups}.

    Returns:
        Confirmation naming the tools that are now available.
    """
    # Execution lives in KaraSession.handle_message because activation mutates
    # session state. This body only exists so the schema is built the same way
    # as every other tool; reaching it means the interception was bypassed.
    raise RuntimeError("activate_tool_group must be handled by KaraSession.")


# This docstring becomes the tool schema the model reads, so the group list has
# to be derived rather than restated. Hardcoded, it silently taught the model a
# stale enum the moment a module was added, renamed, or promoted to always-on --
# the model would keep asking for a group that no longer exists, or never learn
# about one that does.
activate_tool_group.__doc__ = (activate_tool_group.__doc__ or "").format(
    groups=", ".join(ON_DEMAND_GROUP_ORDER)
)


ACTIVATE_SCHEMA: dict[str, Any] = tool_schemas.function_to_tool(activate_tool_group)


def group_of(tool_name: str) -> str | None:
    return _GROUP_OF.get(tool_name)


def is_read_only(tool_name: str) -> bool:
    """True if the tool has no side effects and is safe to run concurrently."""
    return tool_name in READ_ONLY


_KEYWORD_PATTERNS: dict[str, re.Pattern[str]] = {
    group: re.compile(
        r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b", re.IGNORECASE
    )
    for group, words in GROUP_KEYWORDS.items()
    if words and group in GROUPS
}


def groups_for_text(text: str) -> set[str]:
    """On-demand groups whose keywords appear in the given message."""
    if not text:
        return set()
    return {
        group
        for group, pattern in _KEYWORD_PATTERNS.items()
        if group in ON_DEMAND_GROUPS and pattern.search(text)
    }


def schemas_for_groups(groups: set[str] | frozenset[str]) -> list[dict[str, Any]]:
    """Tool schemas for the given groups, in stable registry order.

    Appends ``activate_tool_group`` whenever something is still hidden, so the
    model always has a way to reach a capability the keywords missed.
    """
    names = {name for group in groups for name in GROUPS.get(group, ())}
    schemas = [item for item in TOOL_SCHEMAS if item["function"]["name"] in names]
    if set(GROUPS) - set(groups):
        schemas.append(ACTIVATE_SCHEMA)
    return schemas
