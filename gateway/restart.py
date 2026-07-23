"""Gateway restart, update detection, and self-replacement.

STUDY GUIDE
-----------
* Detects code changes via SHA-256 fingerprint of all .py source files.
* Writes restart flags and spawns a replacement gateway process (hidden on Windows).
* Key concepts: ``Path.rglob``, ``subprocess.Popen``, ``hashlib``, detached processes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import config

log = logging.getLogger("kara.gateway.restart")

SKIP_DIR_NAMES = {".venv", "brain", "__pycache__", ".git", "bin"}
POLL_INTERVAL = float(os.getenv("GATEWAY_POLL_INTERVAL", "10"))


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    # LEARN: os.walk lets us prune directories in-place (dirnames[:] = ...) so
    # skipped trees like .venv are never entered — much faster than rglob + filter,
    # which enumerates thousands of .venv files first and discards them after.
    for dirpath, dirnames, filenames in os.walk(config.PACKAGE_DIR):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for name in filenames:
            if name.endswith(".py"):
                files.append(Path(dirpath) / name)
    return files


# LEARN: Cache per-file hashes keyed by (mtime, size) — unchanged files are not
# re-read from disk on every 10s poll, only stat()ed.
_file_hash_cache: dict[str, tuple[int, int, str]] = {}


def compute_code_fingerprint() -> str:
    h = hashlib.sha256()
    for path in sorted(_iter_source_files(), key=lambda p: str(p)):
        try:
            st = path.stat()
            key = str(path)
            cached = _file_hash_cache.get(key)
            if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                digest = cached[2]
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                _file_hash_cache[key] = (st.st_mtime_ns, st.st_size, digest)
            h.update(str(path.relative_to(config.PACKAGE_DIR)).encode())
            h.update(digest.encode())
        except OSError:
            pass
    return h.hexdigest()[:16]


def load_stored_fingerprint() -> str:
    if config.CODE_FINGERPRINT_FILE.exists():
        return config.CODE_FINGERPRINT_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_fingerprint(fp: str) -> None:
    config.ensure_brain()
    config.CODE_FINGERPRINT_FILE.write_text(fp, encoding="utf-8")


def request_restart(reason: str = "manual") -> None:
    config.ensure_brain()
    config.RESTART_FLAG.write_text(f"{time.time()}:{reason}\n", encoding="utf-8")
    log.info("Restart requested (%s)", reason)


def queue_restart_notification(chat_id: int) -> None:
    """Remember a Telegram chat to ping once the replacement gateway is online."""
    import json

    config.ensure_brain()
    chat_ids: list[int] = []
    if config.RESTART_NOTIFY_FILE.exists():
        try:
            data = json.loads(config.RESTART_NOTIFY_FILE.read_text(encoding="utf-8"))
            chat_ids = [int(x) for x in data.get("chat_ids", [])]
        except (json.JSONDecodeError, TypeError, ValueError):
            chat_ids = []
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
    config.RESTART_NOTIFY_FILE.write_text(
        json.dumps({"chat_ids": chat_ids}), encoding="utf-8"
    )


def consume_restart_notifications() -> list[int]:
    """Return queued chat ids and clear the notify file."""
    import json

    if not config.RESTART_NOTIFY_FILE.exists():
        return []
    try:
        data = json.loads(config.RESTART_NOTIFY_FILE.read_text(encoding="utf-8"))
        chat_ids = [int(x) for x in data.get("chat_ids", [])]
    except (json.JSONDecodeError, TypeError, ValueError):
        chat_ids = []
    config.RESTART_NOTIFY_FILE.unlink(missing_ok=True)
    return chat_ids


def clear_restart_flag() -> None:
    if config.RESTART_FLAG.exists():
        config.RESTART_FLAG.unlink()


def restart_requested() -> bool:
    return config.RESTART_FLAG.exists()


def code_updated() -> bool:
    current = compute_code_fingerprint()
    stored = load_stored_fingerprint()
    if not stored:
        save_fingerprint(current)
        return False
    return current != stored


def write_pid() -> None:
    config.ensure_brain()
    config.GATEWAY_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid() -> None:
    if config.GATEWAY_PID_FILE.exists():
        config.GATEWAY_PID_FILE.unlink()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def claim_restart_leadership() -> bool:
    """Only one gateway instance may orchestrate a restart/spawn."""
    config.ensure_brain()
    if config.RESTART_LOCK_FILE.exists():
        try:
            raw = config.RESTART_LOCK_FILE.read_text(encoding="utf-8").strip()
            pid = int(raw.split(":", 1)[0])
        except (OSError, ValueError):
            pid = -1
        if _pid_alive(pid):
            return False
        config.RESTART_LOCK_FILE.unlink(missing_ok=True)
    try:
        fd = os.open(
            str(config.RESTART_LOCK_FILE),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}:{time.time()}\n")
        return True
    except FileExistsError:
        return False


def release_restart_leadership() -> None:
    if not config.RESTART_LOCK_FILE.exists():
        return
    try:
        raw = config.RESTART_LOCK_FILE.read_text(encoding="utf-8").strip()
        pid = int(raw.split(":", 1)[0])
    except (OSError, ValueError):
        pid = -1
    if pid in (-1, os.getpid()):
        config.RESTART_LOCK_FILE.unlink(missing_ok=True)


def acquire_instance_lock() -> bool:
    """Ensure only one gateway polls Telegram at a time."""
    config.ensure_brain()
    if config.GATEWAY_INSTANCE_LOCK.exists():
        try:
            raw = config.GATEWAY_INSTANCE_LOCK.read_text(encoding="utf-8").strip()
            pid = int(raw.split(":", 1)[0])
        except (OSError, ValueError):
            pid = -1
        if _pid_alive(pid) and pid != os.getpid():
            return False
        config.GATEWAY_INSTANCE_LOCK.unlink(missing_ok=True)
    try:
        fd = os.open(
            str(config.GATEWAY_INSTANCE_LOCK),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}:{time.time()}\n")
        return True
    except FileExistsError:
        return False


def release_instance_lock() -> None:
    if not config.GATEWAY_INSTANCE_LOCK.exists():
        return
    try:
        raw = config.GATEWAY_INSTANCE_LOCK.read_text(encoding="utf-8").strip()
        pid = int(raw.split(":", 1)[0])
    except (OSError, ValueError):
        pid = -1
    if pid in (-1, os.getpid()):
        config.GATEWAY_INSTANCE_LOCK.unlink(missing_ok=True)


def spawn_replacement() -> None:
    """Start a fresh gateway process hidden on Windows (no console window)."""
    run_script = config.PACKAGE_DIR / "scripts" / "run_gateway.py"
    if sys.platform == "win32":
        exe = config.PACKAGE_DIR / ".venv" / "Scripts" / "python.exe"
        if not exe.exists():
            exe = Path(sys.executable)
        # LEARN: uv-venv python.exe is a trampoline that spawns the real interpreter
        # as a child. CREATE_NO_WINDOW gives it an *invisible* console the child
        # inherits — no window opens, and closing terminals can't kill Kara.
        # (DETACHED_PROCESS would make the child open its own visible console.)
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        exe = Path(sys.executable)
        creationflags = 0

    subprocess.Popen(
        [str(exe), str(run_script)],
        cwd=str(config.PACKAGE_DIR),
        creationflags=creationflags,
        close_fds=(sys.platform != "win32"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log.info("Spawned replacement gateway process.")
