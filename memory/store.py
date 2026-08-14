"""Kara's brain: file-backed memory (all markdown).

Three tiers:
  * core     - small, always-in-context blocks (persona/human/active_task)
  * learnings- durable facts & insights, one .md per learning
  * sessions - episodic logs of conversations, one .md per session

This module handles reading/writing the files. Semantic retrieval over
learnings + sessions lives in ``vector_index.py``.

A one-time migration pulls any legacy ``memory/core_memory.json`` into the new
markdown core files so nothing is lost from the previous version.

STUDY GUIDE
-----------
* Reads/writes markdown files for core memory, learnings, and session logs.
* Migrates old JSON core memory to markdown on first run.
* Key concepts: Path I/O, ``with open`` append mode, regex slugify, graceful try/except.
"""
import json
import re
from datetime import datetime
from pathlib import Path

import config

VALID_SECTIONS = ("persona", "human", "active_task")

# These read config.USER_NAME at call time rather than at import, so the name is
# resolved after .env is loaded and stays patchable in tests. Set KARA_USER_NAME
# to have Kara address you by name; the default keeps the sentences grammatical.
def default_human() -> str:
    # Only claim to know a name when one was actually configured.
    if config.USER_NAME == config.DEFAULT_USER_NAME:
        opening = "The user has not introduced themselves yet."
    else:
        opening = f"The user is {config.USER_NAME}."
    return f"{opening} They take notes, track projects, and park ideas."


def _default_persona_base() -> str:
    # "the user's" and "Ada's" both read correctly, so no special case needed.
    return (
        f"You are Kara, {config.USER_NAME}'s personal AI assistant. Your entire memory lives in "
        "your local 'brain' directory: core memory (always in context), learnings "
        "(durable facts you've saved), and sessions (logs of past conversations). "
        "You operate via a CLI and manage your own memory."
    )


def _read(path: Path) -> str:
    # LEARN: try/except on file read returns "" if missing — callers don't need to check exists().
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _slugify(text: str) -> str:
    # LEARN: re.sub replaces non-alphanumeric runs with hyphens for safe filenames.
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "note"


def _default_persona() -> str:
    seed = _read(config.IDENTITY_SEED)
    base = _default_persona_base()
    return f"{seed}\n\n{base}" if seed else base


def _migrate_legacy_core() -> None:
    """Import legacy memory/core_memory.json into markdown core files, once."""
    legacy = config.REPO_ROOT / "memory" / "core_memory.json"
    if not legacy.exists():
        return
    if config.CORE_FILES["persona"].exists():
        return  # already migrated / initialized
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception:
        return
    for section in VALID_SECTIONS:
        value = str(data.get(section, "")).strip()
        if value:
            _write(config.CORE_FILES[section], value)


def init_core_memory() -> None:
    """Seed any missing core memory files with sensible defaults."""
    config.ensure_brain()
    _migrate_legacy_core()
    try:
        migrate_legacy_session_logs()
    except Exception:
        pass  # a migration failure must never block the chat loop
    defaults = {
        "persona": _default_persona(),
        "human": default_human(),
        "active_task": "None",
    }
    for section, path in config.CORE_FILES.items():
        if not path.exists():
            _write(path, defaults[section])


def get_core_section(section: str) -> str:
    return _read(config.CORE_FILES[section])


def set_core_section(section: str, content: str) -> None:
    _write(config.CORE_FILES[section], content.strip())


def render_core_memory() -> str:
    """Render the core block injected into the system prompt every turn."""
    init_core_memory()
    persona = get_core_section("persona")
    human = get_core_section("human")
    task = get_core_section("active_task") or "None"
    return (
        f"{persona}\n\n"
        f"HUMAN CONTEXT:\n{human}\n\n"
        f"CURRENT ACTIVE TASK:\n{task}"
    )


def save_learning(title: str, content: str) -> Path:
    """Write a durable learning as its own timestamped markdown file."""
    config.ensure_brain()
    stamp = datetime.now().strftime("%Y-%m-%d")
    filename = f"{stamp}-{_slugify(title)}.md"
    path = config.LEARNINGS_DIR / filename
    # LEARN: while path.exists() avoids overwriting — appends -2, -3, etc. to filename.
    counter = 2
    while path.exists():
        path = config.LEARNINGS_DIR / f"{stamp}-{_slugify(title)}-{counter}.md"
        counter += 1
    body = (
        f"# {title}\n\n"
        f"_Saved: {datetime.now().isoformat(timespec='seconds')}_\n\n"
        f"{content.strip()}\n"
    )
    _write(path, body)
    return path


_SUMMARY_HEADING = re.compile(r"^##\s+Summary\s*$", re.MULTILINE)


def _extract_legacy_summary(text: str) -> str:
    """Pull the ``## Summary`` block out of a pre-migration session log."""
    match = _SUMMARY_HEADING.search(text)
    return text[match.end():].strip() if match else ""


def migrate_legacy_session_logs() -> int:
    """Import summaries from old ``brain/sessions/*.md`` logs into SQLite, once.

    Conversation transcripts now live only in SQLite and only summaries are
    indexed, so the old markdown logs are no longer read. Their summaries are
    still worth keeping, so lift those across and leave the files on disk —
    they are the user's data and this must not delete them.
    """
    if config.SESSIONS_MIGRATED_MARKER.exists():
        return 0

    from memory import session_db

    config.ensure_brain()
    imported = 0
    for path in sorted(config.SESSIONS_DIR.glob("*.md")):
        summary = _extract_legacy_summary(_read(path))
        if not summary:
            continue  # raw chatter with no recap — exactly what we stopped indexing
        session_key = f"legacy:{path.stem}"
        if session_db.has_session_summary(session_key, summary):
            continue
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                timespec="seconds"
            )
        except OSError:
            stamp = None
        session_db.save_session_summary(
            session_key, f"Session {path.stem}", summary, created_at=stamp
        )
        imported += 1

    _write(config.SESSIONS_MIGRATED_MARKER, datetime.now().isoformat(timespec="seconds"))
    return imported
