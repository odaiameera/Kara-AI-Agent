"""Agent-facing memory tools.

Thin wrappers around ``memory_store`` and ``vector_index`` that Kara calls to
manage their own brain: edit core memory, save durable learnings, and search their
learnings + sessions semantically (the MemGPT / Letta pattern).

STUDY GUIDE
-----------
* Exposes memory operations as plain Python functions the LLM invokes as tools.
* Validates section names and formats human-readable confirmation strings.
* Key concepts: docstrings for tool schema generation, input validation, string formatting.
"""
import memory_store
import vector_index

VALID_SECTIONS = memory_store.VALID_SECTIONS


def core_memory_append(section: str, content: str) -> str:
    """
    Append a new fact or line to one of your always-in-context core memory sections. Persists across sessions.

    Args:
        section: Which block to update. One of: 'persona', 'human', 'active_task'.
        content: The new line/fact to add to that section.

    Returns:
        A confirmation showing the updated section contents.
    """
    # LEARN: Tool functions return strings — the LLM reads the result as tool output.
    section = section.lower().strip()
    if section not in VALID_SECTIONS:
        return f"Error: invalid section '{section}'. Valid: {', '.join(VALID_SECTIONS)}."

    existing = memory_store.get_core_section(section)
    if existing and existing != "None":
        updated = f"{existing}\n{content.strip()}".strip()
    else:
        updated = content.strip()
    memory_store.set_core_section(section, updated)
    return f"Core memory updated. '{section}' is now:\n{updated}"


def core_memory_replace(section: str, old_content: str, new_content: str) -> str:
    """
    Replace text within one of your core memory sections. Use this to correct or update a stored fact. Persists across sessions.

    Args:
        section: Which block to update. One of: 'persona', 'human', 'active_task'.
        old_content: The exact text to find and replace.
        new_content: The text to replace it with.

    Returns:
        A confirmation showing the updated section, or an error if old_content was not found.
    """
    section = section.lower().strip()
    if section not in VALID_SECTIONS:
        return f"Error: invalid section '{section}'. Valid: {', '.join(VALID_SECTIONS)}."

    existing = memory_store.get_core_section(section)
    if old_content not in existing:
        return f"Error: could not find '{old_content}' in the '{section}' section."
    updated = existing.replace(old_content, new_content).strip()
    memory_store.set_core_section(section, updated)
    return f"Core memory updated. '{section}' is now:\n{updated}"


def set_active_task(task: str) -> str:
    """
    Set the task you're currently helping the user with. Persists across sessions. Pass an empty string or 'None' to clear it.

    Args:
        task: A short description of the current task, or empty to clear.

    Returns:
        A confirmation of the newly set active task.
    """
    value = task.strip() or "None"
    memory_store.set_core_section("active_task", value)
    return f"Active task set to: {value}"


def save_learning(title: str, content: str) -> str:
    """
    Save a durable learning or insight to long-term memory as its own note. Use this for facts worth remembering beyond core memory (preferences, decisions, project details). Searchable later via search_memory.

    Args:
        title: A short title for the learning.
        content: The full content of the learning.

    Returns:
        A confirmation with the saved file name.
    """
    path = memory_store.save_learning(title, content)
    return f"Saved learning to brain/learnings/{path.name}."


def search_memory(query: str, top_k: int = 5) -> str:
    """
    Search your long-term memory (past learnings and previous conversation sessions) by meaning, not just keywords. Use this to recall things the user told you before or decisions made in earlier sessions.

    Args:
        query: What you want to recall, in natural language.
        top_k: How many results to return (default 5).

    Returns:
        The most relevant snippets from your learnings and sessions.
    """
    # LEARN: Defensive int conversion — LLM may pass top_k as string or invalid value.
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5
    top_k = max(1, min(top_k, 20))
    outcome = vector_index.search(query, top_k=top_k)
    results = outcome["results"]
    if not results:
        base = "No relevant memories found."
        return f"{base} ({outcome['note']})" if outcome.get("note") else base

    lines = []
    if outcome.get("note"):
        lines.append(f"[{outcome['note']}]")
    lines.append(f"Top {len(results)} memories for '{query}':")
    for r in results:
        snippet = r["text"].strip().replace("\n", " ")
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."
        lines.append(f"- ({r['source']}) {r['title']}: {snippet}")
    return "\n".join(lines)

# --- Registry declaration ------------------------------------------------------
# Consumed by tools.registry; this is the single source of truth for which
# functions in this module are exposed to the model and which of them are safe
# for unattended scheduled runs.
TOOL_GROUP = "memory"

TOOLS = [
    core_memory_append,
    core_memory_replace,
    set_active_task,
    save_learning,
    search_memory,
]

SCHEDULED_SAFE = {
    "search_memory",
}

# Tools with no side effects. Used to decide what may run concurrently; a
# superset of SCHEDULED_SAFE, which is a separate policy about unattended runs.
READ_ONLY = {
    "search_memory",
}
