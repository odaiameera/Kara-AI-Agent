"""SQLite session store — conversation history survives gateway restarts.

STUDY GUIDE
-----------
* Persists chat messages and session metadata in ``brain/state.db``.
* Uses a context manager for safe connect/commit/close on every database operation.
* Key concepts: ``@contextmanager``, ``with`` statements, SQLite placeholders ``?``, ``sqlite3.Row``.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import config

DB_PATH = config.BRAIN_DIR / "state.db"

# LEARN: Module-level flag makes init_db() run its DDL only once per process —
# repeated calls from hot paths (every message append) become free no-ops.
_initialized = False


def _now() -> str:
    # LEARN: timezone.utc gives aware UTC datetimes; isoformat() stores a sortable string.
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn():
    # LEARN: @contextmanager turns a generator with yield into a ``with`` block resource manager.
    # Code before yield runs on enter; finally always runs on exit (even on exceptions).
    config.ensure_brain()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    global _initialized
    if _initialized:
        return
    # LEARN: executescript runs multiple SQL statements; CREATE IF NOT EXISTS is idempotent.
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                provider_id TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                interrupted INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                tool_call_id TEXT,
                FOREIGN KEY (session_key) REFERENCES sessions(session_key)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_key, seq);
            CREATE TABLE IF NOT EXISTS session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_session_summaries_created
                ON session_summaries(created_at);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "tool_call_id" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
        _backfill_legacy_tool_call_ids_in_conn(conn)
    _initialized = True


def _backfill_legacy_tool_call_ids_in_conn(conn: sqlite3.Connection) -> None:
    """Link pre-migration tool rows to their preceding assistant call IDs.

    Older Kara sessions stored assistant ``tool_calls`` but not the matching ID
    on each following ``tool`` message. Responses requires that correlation on
    replay, so backfill in the original call/result order once and persist it.
    """
    rows = conn.execute(
        "SELECT id, session_key, role, tool_calls, tool_call_id FROM messages "
        "ORDER BY session_key, seq"
    ).fetchall()
    current_session = None
    pending_call_ids: list[str] = []
    for row in rows:
        if row["session_key"] != current_session:
            current_session = row["session_key"]
            pending_call_ids = []
        if row["role"] == "assistant":
            pending_call_ids = []
            try:
                calls = json.loads(row["tool_calls"]) if row["tool_calls"] else []
            except (TypeError, json.JSONDecodeError):
                calls = []
            for call in calls if isinstance(calls, list) else []:
                call_id = call.get("id") if isinstance(call, dict) else None
                if isinstance(call_id, str) and call_id:
                    pending_call_ids.append(call_id)
        elif row["role"] == "tool" and not row["tool_call_id"] and pending_call_ids:
            conn.execute(
                "UPDATE messages SET tool_call_id=? WHERE id=?",
                (pending_call_ids.pop(0), row["id"]),
            )
        elif row["role"] == "user":
            pending_call_ids = []


def backfill_legacy_tool_call_ids() -> None:
    """Public, idempotent migration for tests and existing brain databases."""
    init_db()
    with _conn() as conn:
        _backfill_legacy_tool_call_ids_in_conn(conn)


def build_session_key(platform: str, user_id: int | str) -> str:
    return f"kara:{platform}:user:{user_id}"


def ensure_session(
    session_key: str,
    channel: str,
    provider_id: str,
    model: str,
) -> None:
    init_db()
    now = _now()
    # LEARN: ON CONFLICT ... DO UPDATE is SQLite upsert — insert or update if key exists.
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_key, channel, provider_id, model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                provider_id=excluded.provider_id,
                model=excluded.model,
                updated_at=excluded.updated_at
            """,
            (session_key, channel, provider_id, model, now, now),
        )


def update_session_model(session_key: str, provider_id: str, model: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET provider_id=?, model=?, updated_at=? WHERE session_key=?",
            (provider_id, model, _now(), session_key),
        )


def clear_messages(session_key: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_key=?", (session_key,))


def load_messages(session_key: str) -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls, tool_name, tool_call_id FROM messages "
            "WHERE session_key=? ORDER BY seq",
            (session_key,),
        ).fetchall()

    # LEARN: Rebuild Python dicts from rows; json.loads turns stored JSON strings back into lists.
    messages: list[dict[str, Any]] = []
    for row in rows:
        msg: dict[str, Any] = {"role": row["role"]}
        if row["content"]:
            msg["content"] = row["content"]
        if row["tool_calls"]:
            msg["tool_calls"] = json.loads(row["tool_calls"])
        if row["tool_name"]:
            msg["tool_name"] = row["tool_name"]
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        messages.append(msg)
    return messages


def append_message(session_key: str, msg: dict[str, Any]) -> None:
    init_db()
    with _conn() as conn:
        # LEARN: COALESCE(MAX(seq), -1) + 1 assigns the next sequence number per session.
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_key=?",
            (session_key,),
        ).fetchone()[0]
        tool_calls = msg.get("tool_calls")
        conn.execute(
            """
            INSERT INTO messages (session_key, seq, role, content, tool_calls, tool_name, tool_call_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key,
                seq,
                msg.get("role", "user"),
                msg.get("content") or "",
                json.dumps(tool_calls) if tool_calls else None,
                msg.get("tool_name"),
                msg.get("tool_call_id"),
            ),
        )
        conn.execute(
            "UPDATE sessions SET updated_at=? WHERE session_key=?",
            (_now(), session_key),
        )


def mark_interrupted(session_key: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET interrupted=1, updated_at=? WHERE session_key=?",
            (_now(), session_key),
        )


def clear_interrupted(session_key: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET interrupted=0, updated_at=? WHERE session_key=?",
            (_now(), session_key),
        )


def is_interrupted(session_key: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT interrupted FROM sessions WHERE session_key=?",
            (session_key,),
        ).fetchone()
    return bool(row and row["interrupted"])


# --- Session summaries ---------------------------------------------------------
# What Kara remembers about a past conversation. Raw turns stay in `messages` for
# replay; only these summaries feed semantic recall, so search returns decisions
# rather than chatter.


def save_session_summary(
    session_key: str, title: str, summary: str, *, created_at: str | None = None
) -> int:
    """Store one end-of-session summary. Returns its row id."""
    init_db()
    text = summary.strip()
    if not text:
        return 0
    with _conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO session_summaries (session_key, title, summary, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_key, title.strip() or session_key, text, created_at or _now()),
        )
        return int(cursor.lastrowid or 0)


def load_session_summaries() -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, session_key, title, summary, created_at FROM session_summaries "
            "ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def session_summary_fingerprint() -> tuple[int, int, str]:
    """Cheap change detector for the vector index: (count, max id, max created_at)."""
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS max_id, "
            "COALESCE(MAX(created_at), '') AS max_created FROM session_summaries"
        ).fetchone()
    return (int(row["n"]), int(row["max_id"]), str(row["max_created"]))


def has_session_summary(session_key: str, summary: str) -> bool:
    """True if this exact summary is already stored (keeps migration idempotent)."""
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM session_summaries WHERE session_key=? AND summary=? LIMIT 1",
            (session_key, summary.strip()),
        ).fetchone()
    return row is not None
