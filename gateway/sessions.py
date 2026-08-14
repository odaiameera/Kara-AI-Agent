"""Gateway session management — SQLite-backed KaraSession cache.

STUDY GUIDE
-----------
* Keeps one ``KaraSession`` object per user in memory for fast Telegram replies.
* Creates fresh sessions on /new and gracefully ends all on gateway shutdown.
* Key concepts: module-level dict cache, ``dict.pop``, ``list(_active.values())`` copy.
"""
from __future__ import annotations

import logging
import threading

from memory import session_db
from kara import KaraSession

log = logging.getLogger("kara.gateway.sessions")

# LEARN: Module-level dict acts as an in-memory cache keyed by session_key string.
_active: dict[str, KaraSession] = {}

# One turn at a time per session. The gateway processes updates concurrently so
# that /stop can be received while a turn is running, which means two messages
# from the same user could otherwise mutate the same KaraSession.messages list
# at once.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def turn_lock(session_key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_key, threading.Lock())


def request_stop(session_key: str) -> bool:
    """Ask a running turn to stop. False if that session is not active."""
    session = _active.get(session_key)
    if session is None:
        return False
    session.request_stop()
    return True


def get_session(session_key: str, channel: str = "telegram") -> KaraSession:
    if session_key not in _active:
        _active[session_key] = KaraSession(session_key, channel=channel)
        if session_db.is_interrupted(session_key):
            log.info("Resumed interrupted session %s", session_key)
            session_db.clear_interrupted(session_key)
    return _active[session_key]


def new_session(session_key: str, channel: str = "telegram") -> KaraSession:
    # LEARN: pop(key, None) removes and returns old session, or None if missing.
    old = _active.pop(session_key, None)
    if old:
        old.end_session()
    session = KaraSession(session_key, channel=channel, fresh=True)
    _active[session_key] = session
    return session


def shutdown_all() -> None:
    # LEARN: list(...) copies values so we can clear _active while iterating safely.
    for session in list(_active.values()):
        try:
            session.end_session()
        except Exception:
            pass
    _active.clear()
