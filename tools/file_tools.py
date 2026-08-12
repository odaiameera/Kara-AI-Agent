"""Bounded local filesystem tools for Kara.

Read/search access defaults to Kara's project plus the user's home directory.
Write access defaults to the project only. Both can be changed with path-list
environment variables, while credential stores remain blocked unless the user
explicitly opts in.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import config

MAX_READ_CHARS = int(os.getenv("KARA_FILE_MAX_READ_CHARS", "30000"))
MAX_SEARCH_SECONDS = float(os.getenv("KARA_FILE_SEARCH_TIMEOUT", "12"))
MAX_CONTENT_FILE_BYTES = int(os.getenv("KARA_FILE_SEARCH_MAX_FILE_BYTES", "1000000"))

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "appdata",
    "$recycle.bin",
    "system volume information",
}
_SENSITIVE_DIRS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".codex",
    ".hermes",
    "1password",
}
_SENSITIVE_FILES = {
    ".env",
    "auth.json",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
_TEXT_EXTENSIONS = {
    "",
    ".txt",
    ".md",
    ".rst",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".html",
    ".css",
    ".sql",
    ".ps1",
    ".bat",
    ".cmd",
    ".sh",
    ".log",
}


def _sensitive_access_enabled() -> bool:
    return os.getenv("KARA_ALLOW_SENSITIVE_FILES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_sensitive(path: Path) -> bool:
    if _sensitive_access_enabled():
        return False
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return bool(parts & _SENSITIVE_DIRS) or name in _SENSITIVE_FILES or name.startswith(
        "credentials."
    )


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _resolve_path(raw_path: str, roots: tuple[Path, ...], *, purpose: str) -> Path:
    if not raw_path.strip():
        raise ValueError(f"A path is required for {purpose}.")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        if not roots:
            raise ValueError(f"No allowed roots are configured for {purpose}.")
        candidate = roots[0] / candidate
    candidate = candidate.resolve(strict=False)
    if not _within(candidate, roots):
        allowed = "; ".join(str(root) for root in roots) or "(none)"
        raise PermissionError(
            f"Path is outside KARA_FILE_{purpose.upper()}_ROOTS. Allowed roots: {allowed}"
        )
    if _is_sensitive(candidate):
        raise PermissionError(
            "Sensitive credential/profile paths are blocked. Set "
            "KARA_ALLOW_SENSITIVE_FILES=1 only if you intentionally want to expose them."
        )
    return candidate


def _relative_display(path: Path) -> str:
    for root in config.FILE_READ_ROOTS:
        if path == root or path.is_relative_to(root):
            suffix = path.relative_to(root)
            return f"{root} :: {suffix}" if str(suffix) != "." else str(root)
    return str(path)


def file_info(path: str) -> str:
    """Return structured metadata for an allowed local file or directory.

    Args:
        path: Absolute allowed path, or a path relative to Kara's project.

    Returns:
        JSON metadata including resolved path, kind, size, extension, and timestamps.
    """
    try:
        target = _resolve_path(path, config.FILE_READ_ROOTS, purpose="read")
        if not target.exists():
            return json.dumps({"ok": False, "error": f"Path does not exist: {target}"})
        stat = target.stat()
        payload = {
            "ok": True,
            "file": {
                "path": str(target),
                "name": target.name,
                "kind": "directory" if target.is_dir() else "file",
                "extension": target.suffix.casefold(),
                "size_bytes": stat.st_size,
                "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "created_utc": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(),
                "read_only": not os.access(target, os.W_OK),
            },
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)
    except (ValueError, PermissionError, OSError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def list_directory(path: str = ".", max_entries: int = 100) -> str:
    """List files and folders in an allowed local directory (non-recursive).

    Args:
        path: Absolute allowed path, or a path relative to Kara's project.
        max_entries: Maximum entries to return (1-500).

    Returns:
        A compact directory listing with entry type and file size.
    """
    try:
        target = _resolve_path(path, config.FILE_READ_ROOTS, purpose="read")
    except (ValueError, PermissionError) as exc:
        return f"Error: {exc}"
    if not target.exists():
        return f"Error: Path does not exist: {target}"
    if not target.is_dir():
        return f"Error: Path is not a directory: {target}"
    limit = max(1, min(int(max_entries), 500))
    rows: list[str] = []
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        for entry in entries:
            if _is_sensitive(entry):
                continue
            if entry.is_dir():
                rows.append(f"[dir]  {entry.name}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = -1
                rows.append(f"[file] {entry.name} ({size} bytes)")
            if len(rows) >= limit:
                break
    except OSError as exc:
        return f"Error listing {target}: {exc}"
    suffix = "\n... (truncated)" if len(rows) >= limit else ""
    return f"Directory: {target}\n" + ("\n".join(rows) or "(empty)") + suffix


def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """Read a UTF-8/text file from an allowed local path with line limits.

    Args:
        path: Absolute allowed path, or a path relative to Kara's project.
        start_line: First 1-based line to return.
        max_lines: Maximum lines to return (1-2000).

    Returns:
        Numbered file content, truncated to safe response limits.
    """
    try:
        target = _resolve_path(path, config.FILE_READ_ROOTS, purpose="read")
    except (ValueError, PermissionError) as exc:
        return f"Error: {exc}"
    if not target.exists() or not target.is_file():
        return f"Error: File does not exist: {target}"
    first = max(1, int(start_line))
    limit = max(1, min(int(max_lines), 2000))
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return f"Error reading {target}: {exc}"
    if b"\x00" in raw[:8192]:
        return f"Error: Binary files are not supported: {target}"
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    selected = lines[first - 1 : first - 1 + limit]
    numbered = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, first))
    if len(numbered) > MAX_READ_CHARS:
        numbered = numbered[:MAX_READ_CHARS] + "\n... (character limit reached)"
    remaining = max(0, len(lines) - (first - 1 + len(selected)))
    tail = f"\n... ({remaining} more lines)" if remaining else ""
    return f"File: {target}\n{numbered or '(empty)'}{tail}"


def write_file(path: str, content: str, mode: str = "create") -> str:
    """Create, overwrite, or append a UTF-8 file inside an allowed write root.

    Args:
        path: Absolute allowed path, or a path relative to Kara's project.
        content: Exact text to write.
        mode: create (fail if present), overwrite, or append.

    Returns:
        A confirmation containing the resolved path and bytes written.
    """
    try:
        target = _resolve_path(path, config.FILE_WRITE_ROOTS, purpose="write")
    except (ValueError, PermissionError) as exc:
        return f"Error: {exc}"
    selected_mode = mode.strip().lower()
    if selected_mode not in {"create", "overwrite", "append"}:
        return "Error: mode must be create, overwrite, or append."
    if target.exists() and target.is_dir():
        return f"Error: Path is a directory: {target}"
    if selected_mode == "create" and target.exists():
        return f"Error: File already exists; use mode='overwrite' or 'append': {target}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if selected_mode == "append":
            with target.open("a", encoding="utf-8", newline="") as handle:
                handle.write(content)
        else:
            target.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        return f"Error writing {target}: {exc}"
    return f"Wrote {len(content.encode('utf-8'))} bytes to {target} ({selected_mode})."


def copy_file(source: str, destination: str, overwrite: bool = False) -> str:
    """Copy one allowed file into an allowed write root.

    Args:
        source: Existing file inside an allowed read root.
        destination: New path inside an allowed write root.
        overwrite: Replace an existing destination only when explicitly true.

    Returns:
        JSON confirmation with resolved source, destination, and byte size.
    """
    try:
        src = _resolve_path(source, config.FILE_READ_ROOTS, purpose="read")
        dst = _resolve_path(destination, config.FILE_WRITE_ROOTS, purpose="write")
        if not src.exists() or not src.is_file():
            raise ValueError(f"Source file does not exist: {src}")
        if dst.exists() and not overwrite:
            raise FileExistsError(f"Destination already exists: {dst}")
        if dst.exists() and dst.is_dir():
            raise IsADirectoryError(f"Destination is a directory: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return json.dumps(
            {
                "ok": True,
                "source": str(src),
                "destination": str(dst),
                "size_bytes": dst.stat().st_size,
                "overwritten": bool(overwrite),
            },
            indent=2,
            ensure_ascii=False,
        )
    except (ValueError, PermissionError, OSError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def move_file(source: str, destination: str, overwrite: bool = False) -> str:
    """Move or rename one file entirely within allowed write roots.

    Args:
        source: Existing file inside an allowed write root.
        destination: New path inside an allowed write root.
        overwrite: Replace an existing destination only when explicitly true.

    Returns:
        JSON confirmation. Moving deletes the original path, so read-only roots are rejected.
    """
    try:
        src = _resolve_path(source, config.FILE_WRITE_ROOTS, purpose="write")
        dst = _resolve_path(destination, config.FILE_WRITE_ROOTS, purpose="write")
        if not src.exists() or not src.is_file():
            raise ValueError(f"Source file does not exist: {src}")
        if dst.exists() and not overwrite:
            raise FileExistsError(f"Destination already exists: {dst}")
        if dst.exists() and dst.is_dir():
            raise IsADirectoryError(f"Destination is a directory: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))
        return json.dumps(
            {
                "ok": True,
                "source": str(src),
                "destination": str(dst),
                "size_bytes": dst.stat().st_size,
                "overwritten": bool(overwrite),
            },
            indent=2,
            ensure_ascii=False,
        )
    except (ValueError, PermissionError, OSError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def replace_in_file(
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
) -> str:
    """Replace exact text in an allowed UTF-8 file only when the match count is expected.

    Args:
        path: Existing text file inside an allowed write root.
        old_text: Exact non-empty text to find.
        new_text: Exact replacement text.
        expected_replacements: Required number of matches before any write occurs (1-10000).

    Returns:
        JSON confirmation or a no-change error when the count differs.
    """
    try:
        target = _resolve_path(path, config.FILE_WRITE_ROOTS, purpose="write")
        if not target.exists() or not target.is_file():
            raise ValueError(f"File does not exist: {target}")
        if not old_text:
            raise ValueError("old_text cannot be empty.")
        expected = int(expected_replacements)
        if expected < 1 or expected > 10_000:
            raise ValueError("expected_replacements must be between 1 and 10000.")
        raw = target.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ValueError("Binary files are not supported by replace_in_file.")
        text = raw.decode("utf-8")
        found = text.count(old_text)
        if found != expected:
            raise ValueError(
                f"Expected {expected} replacement match(es), found {found}; file was not changed."
            )
        updated = text.replace(old_text, new_text)
        target.write_text(updated, encoding="utf-8", newline="")
        return json.dumps(
            {
                "ok": True,
                "path": str(target),
                "replacements": found,
                "size_bytes": target.stat().st_size,
            },
            indent=2,
            ensure_ascii=False,
        )
    except (UnicodeDecodeError, ValueError, PermissionError, OSError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def search_files(
    query: str,
    root: str = "",
    search_content: bool = False,
    max_results: int = 50,
) -> str:
    """Search allowed PC folders by filename and optionally text content.

    Args:
        query: Case-insensitive filename fragment or literal text to find.
        root: Optional allowed directory; blank searches all configured read roots.
        search_content: Also search text-file contents and return matching line previews.
        max_results: Maximum matches to return (1-200).

    Returns:
        Matching paths (and content previews), bounded by count and time.
    """
    needle = query.strip().casefold()
    if not needle:
        return "Error: query cannot be empty."
    limit = max(1, min(int(max_results), 200))
    if root.strip():
        try:
            roots = (_resolve_path(root, config.FILE_READ_ROOTS, purpose="read"),)
        except (ValueError, PermissionError) as exc:
            return f"Error: {exc}"
    else:
        roots = config.FILE_READ_ROOTS

    started = time.monotonic()
    matches: list[str] = []
    timed_out = False
    for search_root in roots:
        if not search_root.exists() or not search_root.is_dir() or _is_sensitive(search_root):
            continue
        for current, dirs, files in os.walk(search_root, topdown=True, onerror=lambda _e: None):
            current_path = Path(current)
            dirs[:] = [
                name
                for name in dirs
                if name.casefold() not in _IGNORED_DIRS
                and not _is_sensitive(current_path / name)
            ]
            for name in files:
                if time.monotonic() - started > MAX_SEARCH_SECONDS:
                    timed_out = True
                    break
                path = current_path / name
                if _is_sensitive(path):
                    continue
                if needle in name.casefold():
                    matches.append(f"[name] {_relative_display(path)}")
                if search_content and path.suffix.casefold() in _TEXT_EXTENSIONS:
                    try:
                        if path.stat().st_size <= MAX_CONTENT_FILE_BYTES:
                            text = path.read_text(encoding="utf-8", errors="ignore")
                            for line_number, line in enumerate(text.splitlines(), 1):
                                index = line.casefold().find(needle)
                                if index >= 0:
                                    preview = line[max(0, index - 60) : index + len(query) + 100].strip()
                                    matches.append(
                                        f"[content:{line_number}] {_relative_display(path)} :: {preview}"
                                    )
                                    break
                    except OSError:
                        pass
                if len(matches) >= limit:
                    break
            if timed_out or len(matches) >= limit:
                break
        if timed_out or len(matches) >= limit:
            break
    if not matches:
        suffix = " (search time limit reached)" if timed_out else ""
        return f"No files found matching '{query}'.{suffix}"
    suffix = "\n... (search time limit reached; results are partial)" if timed_out else ""
    if len(matches) >= limit:
        suffix += "\n... (result limit reached)"
    return f"File search results for '{query}':\n" + "\n".join(matches[:limit]) + suffix

# --- Registry declaration ------------------------------------------------------
# Consumed by tools.registry; this is the single source of truth for which
# functions in this module are exposed to the model and which of them are safe
# for unattended scheduled runs.
TOOL_GROUP = "file"

TOOLS = [
    list_directory,
    read_file,
    write_file,
    search_files,
    file_info,
    copy_file,
    move_file,
    replace_in_file,
]

SCHEDULED_SAFE = {
    "list_directory",
    "read_file",
    "search_files",
    "file_info",
}

# Tools with no side effects. Used to decide what may run concurrently; a
# superset of SCHEDULED_SAFE, which is a separate policy about unattended runs.
READ_ONLY = {
    "list_directory",
    "read_file",
    "search_files",
    "file_info",
}
