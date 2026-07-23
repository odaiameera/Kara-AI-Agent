"""Durable local scheduler storage for Kara reminders and agent jobs."""
from __future__ import annotations

import sqlite3
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

import config

DB_PATH = config.BRAIN_DIR / "scheduler.db"
ONE_SECOND = timedelta(seconds=1)
_initialized = False


@dataclass(frozen=True)
class ScheduledJob:
    id: str
    name: str
    kind: str
    payload: str
    schedule: str
    timezone_name: str
    platform: str
    chat_id: str
    user_id: str
    next_run_at: datetime
    enabled: bool
    status: str
    last_run_at: datetime | None = None
    last_error: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Scheduled times must include a timezone offset.")
    return value.astimezone(timezone.utc)


def _dump_time(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _load_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(timezone.utc) if value else None


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    init_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    global _initialized
    if _initialized and DB_PATH.exists():
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('reminder', 'agent')),
                payload TEXT NOT NULL,
                schedule TEXT NOT NULL,
                timezone_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                next_run_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'scheduled',
                last_run_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
                ON scheduled_jobs(enabled, status, next_run_at);
            """
        )
        # Gateway restarts use at-least-once delivery: a job claimed by a process
        # that exited before completion becomes eligible again on startup.
        conn.execute(
            "UPDATE scheduled_jobs SET status='scheduled' "
            "WHERE enabled=1 AND status='running'"
        )
        conn.commit()
    finally:
        conn.close()
    _initialized = True


def _row_to_job(row: sqlite3.Row) -> ScheduledJob:
    return ScheduledJob(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        payload=row["payload"],
        schedule=row["schedule"],
        timezone_name=row["timezone_name"],
        platform=row["platform"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        next_run_at=_load_time(row["next_run_at"]),  # type: ignore[arg-type]
        enabled=bool(row["enabled"]),
        status=row["status"],
        last_run_at=_load_time(row["last_run_at"]),
        last_error=row["last_error"],
    )


def _next_run(schedule: str, timezone_name: str, now: datetime) -> datetime:
    try:
        local_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    relative = re.fullmatch(
        r"in\s+(\d+)\s*(s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hours?|d|days?)",
        schedule.strip(),
        flags=re.IGNORECASE,
    )
    if relative:
        amount = int(relative.group(1))
        if amount <= 0:
            raise ValueError("Relative schedule must be greater than zero.")
        unit = relative.group(2).casefold()
        if unit.startswith("s"):
            delta = timedelta(seconds=amount)
        elif unit.startswith("m"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("h"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        return _as_utc(now) + delta
    fields = schedule.split()
    if len(fields) == 5:
        if not croniter.is_valid(schedule):
            raise ValueError("Invalid five-field cron expression.")
        local_now = _as_utc(now).astimezone(local_tz)
        next_local = croniter(schedule, local_now).get_next(datetime)
        return _as_utc(next_local)
    try:
        scheduled = datetime.fromisoformat(schedule)
    except ValueError as exc:
        raise ValueError(
            "schedule must be an ISO 8601 timestamp with an offset or a five-field cron expression"
        ) from exc
    scheduled_utc = _as_utc(scheduled)
    if scheduled_utc < _as_utc(now):
        raise ValueError("One-shot schedule must not be in the past.")
    return scheduled_utc


def create_job(
    *,
    name: str,
    kind: str,
    payload: str,
    schedule: str,
    timezone_name: str,
    platform: str,
    chat_id: str,
    user_id: str,
    now: datetime | None = None,
) -> ScheduledJob:
    now = _as_utc(now or _utc_now())
    name = name.strip()
    payload = payload.strip()
    if not name or not payload:
        raise ValueError("name and payload are required.")
    if re.search(
        r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|client[_ -]?secret)\s*[:=]\s*\S+",
        payload,
        flags=re.IGNORECASE,
    ):
        raise ValueError("Scheduled payload appears to contain a secret and was not stored.")
    if kind not in {"reminder", "agent"}:
        raise ValueError("kind must be reminder or agent.")
    next_run_at = _next_run(schedule.strip(), timezone_name.strip(), now)
    job_id = uuid.uuid4().hex[:12]
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO scheduled_jobs
              (id, name, kind, payload, schedule, timezone_name, platform, chat_id,
               user_id, next_run_at, enabled, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'scheduled', ?, ?)
            """,
            (
                job_id, name, kind, payload, schedule.strip(), timezone_name.strip(),
                platform, str(chat_id), str(user_id), _dump_time(next_run_at),
                _dump_time(now), _dump_time(now),
            ),
        )
    job = get_job(job_id)
    assert job is not None
    return job


def get_job(job_id: str) -> ScheduledJob | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(*, user_id: str | None = None, include_finished: bool = False) -> list[ScheduledJob]:
    clauses: list[str] = []
    values: list[object] = []
    if user_id is not None:
        clauses.append("user_id=?")
        values.append(str(user_id))
    if not include_finished:
        clauses.append("status NOT IN ('completed', 'failed')")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM scheduled_jobs{where} ORDER BY next_run_at, created_at",
            values,
        ).fetchall()
    return [_row_to_job(row) for row in rows]


def _owned_job(conn: sqlite3.Connection, job_id: str, user_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise KeyError(f"Unknown scheduled job: {job_id}")
    if row["user_id"] != str(user_id):
        raise PermissionError("Scheduled job belongs to another user.")
    return row


def pause_job(job_id: str, *, user_id: str) -> ScheduledJob:
    with _conn() as conn:
        _owned_job(conn, job_id, user_id)
        conn.execute(
            "UPDATE scheduled_jobs SET enabled=0, status='paused', updated_at=? WHERE id=?",
            (_dump_time(_utc_now()), job_id),
        )
    job = get_job(job_id)
    assert job is not None
    return job


def resume_job(
    job_id: str, *, user_id: str, now: datetime | None = None
) -> ScheduledJob:
    now = _as_utc(now or _utc_now())
    with _conn() as conn:
        row = _owned_job(conn, job_id, user_id)
        next_run = _next_run(row["schedule"], row["timezone_name"], now)
        conn.execute(
            """UPDATE scheduled_jobs SET enabled=1, status='scheduled', next_run_at=?,
               last_error='', updated_at=? WHERE id=?""",
            (_dump_time(next_run), _dump_time(now), job_id),
        )
    job = get_job(job_id)
    assert job is not None
    return job


def delete_job(job_id: str, *, user_id: str) -> None:
    with _conn() as conn:
        _owned_job(conn, job_id, user_id)
        conn.execute("DELETE FROM scheduled_jobs WHERE id=?", (job_id,))


def trigger_job(
    job_id: str, *, user_id: str, now: datetime | None = None
) -> ScheduledJob:
    """Make an owned job due immediately without weakening its execution policy."""
    now = _as_utc(now or _utc_now())
    with _conn() as conn:
        _owned_job(conn, job_id, user_id)
        conn.execute(
            """UPDATE scheduled_jobs SET enabled=1, status='scheduled', next_run_at=?,
               last_error='', updated_at=? WHERE id=?""",
            (_dump_time(now), _dump_time(now), job_id),
        )
    job = get_job(job_id)
    assert job is not None
    return job


def claim_due_jobs(*, now: datetime | None = None, limit: int = 20) -> list[ScheduledJob]:
    now = _as_utc(now or _utc_now())
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT * FROM scheduled_jobs
               WHERE enabled=1 AND status='scheduled' AND next_run_at<=?
               ORDER BY next_run_at LIMIT ?""",
            (_dump_time(now), max(1, min(int(limit), 100))),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE scheduled_jobs SET status='running', updated_at=? WHERE id IN ({placeholders})",
                (_dump_time(now), *ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return [ScheduledJob(**{**_row_to_job(row).__dict__, "status": "running"}) for row in rows]


def complete_job(job_id: str, *, now: datetime | None = None) -> None:
    now = _as_utc(now or _utc_now())
    with _conn() as conn:
        row = conn.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown scheduled job: {job_id}")
        if len(row["schedule"].split()) == 5:
            next_run = _next_run(row["schedule"], row["timezone_name"], now)
            conn.execute(
                """UPDATE scheduled_jobs SET enabled=1, status='scheduled',
                   next_run_at=?, last_run_at=?, last_error='', updated_at=? WHERE id=?""",
                (_dump_time(next_run), _dump_time(now), _dump_time(now), job_id),
            )
        else:
            conn.execute(
                """UPDATE scheduled_jobs
                   SET enabled=0, status='completed', last_run_at=?, last_error='', updated_at=?
                   WHERE id=?""",
                (_dump_time(now), _dump_time(now), job_id),
            )


def fail_job(job_id: str, error: str, *, now: datetime | None = None) -> None:
    """Record a failed run; recurring jobs continue, one-shot jobs stop."""
    now = _as_utc(now or _utc_now())
    safe_error = str(error).strip()[:1000]
    with _conn() as conn:
        row = conn.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown scheduled job: {job_id}")
        if len(row["schedule"].split()) == 5:
            next_run = _next_run(row["schedule"], row["timezone_name"], now)
            conn.execute(
                """UPDATE scheduled_jobs SET enabled=1, status='scheduled',
                   next_run_at=?, last_run_at=?, last_error=?, updated_at=? WHERE id=?""",
                (
                    _dump_time(next_run), _dump_time(now), safe_error,
                    _dump_time(now), job_id,
                ),
            )
        else:
            conn.execute(
                """UPDATE scheduled_jobs SET enabled=0, status='failed',
                   last_run_at=?, last_error=?, updated_at=? WHERE id=?""",
                (_dump_time(now), safe_error, _dump_time(now), job_id),
            )
