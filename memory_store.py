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

DEFAULT_HUMAN = (
    "The user is Odai. They take notes, track projects, and park ideas."
)

_DEFAULT_PERSONA_BASE = (
    "You are Kara, Odai's personal AI assistant. Your entire memory lives in "
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
    return f"{seed}\n\n{_DEFAULT_PERSONA_BASE}" if seed else _DEFAULT_PERSONA_BASE


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
    defaults = {
        "persona": _default_persona(),
        "human": DEFAULT_HUMAN,
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


def start_session() -> Path:
    """Create a new timestamped session log and return its path."""
    config.ensure_brain()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = config.SESSIONS_DIR / f"{stamp}.md"
    header = (
        f"# Session {stamp}\n\n"
        f"_Started: {datetime.now().isoformat(timespec='seconds')}_\n\n"
    )
    _write(path, header)
    return path


def log_turn(session_path: Path, role: str, text: str) -> None:
    """Append a single conversation turn to the session log."""
    if not text:
        return
    try:
        # LEARN: open(..., "a") append mode adds to the file without reading it first.
        with open(session_path, "a", encoding="utf-8") as f:
            f.write(f"**{role}:** {text.strip()}\n\n")
    except Exception:
        pass  # logging must never crash the chat loop


def finalize_session(session_path: Path, summary: str = "") -> None:
    """Append an optional end-of-session summary."""
    if not summary:
        return
    try:
        with open(session_path, "a", encoding="utf-8") as f:
            f.write(f"\n---\n\n## Summary\n\n{summary.strip()}\n")
    except Exception:
        pass
