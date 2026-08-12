"""Optional Obsidian vault bridge.

These are no longer Kara's memory backbone - that lives in the local ``brain/``
directory. They remain as optional tools for reading/writing an *external*
Obsidian vault, active only if OBSIDIAN_VAULT_PATH points to a real folder.

STUDY GUIDE
-----------
* Searches, reads, and writes markdown files in an external Obsidian vault.
* Returns friendly messages when no vault is configured instead of crashing.
* Key concepts: ``Path.rglob``, relative paths, append vs overwrite file modes.
"""
from pathlib import Path

from config import resolve_vault_path

_NO_VAULT_MSG = (
    "No Obsidian vault is configured. Set OBSIDIAN_VAULT_PATH in .env to an "
    "existing folder to enable vault access. (Kara's own memory does not need this.)"
)


def search_obsidian(query: str) -> str:
    """
    Search an optional external Obsidian vault for markdown files containing the keyword query.

    Args:
        query: The keyword or phrase to search for across all notes.

    Returns:
        Matching files with a small preview, or a message if nothing is found / no vault is set.
    """
    vault_path = resolve_vault_path()
    if vault_path is None:
        return _NO_VAULT_MSG

    results = []
    # LEARN: rglob("*.md") walks the entire vault tree; case-insensitive search via .lower().
    for md_file in vault_path.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        lower = content.lower()
        if query.lower() in lower:
            idx = lower.find(query.lower())
            start = max(0, idx - 40)
            end = min(len(content), idx + 40)
            snippet = content[start:end].replace("\n", " ")
            rel_path = md_file.relative_to(vault_path)
            results.append(f'- **{rel_path}**: "...{snippet}..."')
            if len(results) >= 10:
                results.append("... (more results truncated)")
                break

    if not results:
        return f"No notes found matching '{query}'."
    return "Search results:\n" + "\n".join(results)


def read_obsidian_note(file_path: str) -> str:
    """
    Read the full content of a specific note in the external Obsidian vault.

    Args:
        file_path: The relative path to the markdown file inside the vault.

    Returns:
        The content of the markdown file.
    """
    vault_path = resolve_vault_path()
    if vault_path is None:
        return _NO_VAULT_MSG

    full_path = vault_path / file_path
    if not full_path.exists():
        return f"Error: Note '{file_path}' does not exist."
    try:
        return full_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading note: {str(e)}"


def write_obsidian_note(file_path: str, content: str, overwrite: bool = False) -> str:
    """
    Create or append content to a note in the external Obsidian vault.

    Args:
        file_path: The relative path to the markdown file.
        content: The markdown content to write.
        overwrite: If True, overwrites the file. If False, appends to the end.

    Returns:
        A success message indicating the file was saved.
    """
    vault_path = resolve_vault_path()
    if vault_path is None:
        return _NO_VAULT_MSG

    full_path = vault_path / file_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if overwrite:
            full_path.write_text(content, encoding="utf-8")
            return f"Successfully overwrote note at '{file_path}'."
        prefix = ""
        if full_path.exists() and full_path.stat().st_size > 0:
            prefix = "\n\n"
        # LEARN: Append mode preserves existing note content; prefix adds spacing between sections.
        with open(full_path, "a", encoding="utf-8") as f:
            f.write(prefix + content)
        return f"Successfully appended to note at '{file_path}'."
    except Exception as e:
        return f"Error writing to note: {str(e)}"

# --- Registry declaration ------------------------------------------------------
# Consumed by tools.registry; this is the single source of truth for which
# functions in this module are exposed to the model and which of them are safe
# for unattended scheduled runs.
TOOL_GROUP = "obsidian"

TOOLS = [
    search_obsidian,
    read_obsidian_note,
    write_obsidian_note,
]

SCHEDULED_SAFE = {
    "search_obsidian",
    "read_obsidian_note",
}
