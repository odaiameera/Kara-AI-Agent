"""Authenticated conversational tools for Kara's local scheduler."""
from __future__ import annotations

import contextvars
import json
import os
from datetime import datetime
from typing import Any

import scheduler

_DEFAULT_TIMEZONE = os.getenv("KARA_TIMEZONE", "Europe/Dublin").strip() or "Europe/Dublin"
_request_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "kara_scheduler_request", default={}
)


def set_scheduler_request_context(
    *, platform: str, chat_id: int | str, user_id: int | str
) -> contextvars.Token:
    """Bind scheduler mutations to an authenticated gateway update."""
    return _request_context.set(
        {"platform": platform, "chat_id": str(chat_id), "user_id": str(user_id)}
    )


def reset_scheduler_request_context(token: contextvars.Token) -> None:
    _request_context.reset(token)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _context() -> dict[str, str]:
    context = _request_context.get()
    if not all(context.get(key) for key in ("platform", "chat_id", "user_id")):
        raise PermissionError(
            "Scheduler tools require an authenticated gateway conversation."
        )
    return context


def _job_data(job: scheduler.ScheduledJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "name": job.name,
        "kind": job.kind,
        "schedule": job.schedule,
        "timezone": job.timezone_name,
        "next_run_at": job.next_run_at.isoformat(),
        "enabled": job.enabled,
        "status": job.status,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "last_error": job.last_error,
    }


def _create(
    *, name: str, kind: str, payload: str, schedule: str, timezone_name: str
) -> str:
    try:
        context = _context()
        job = scheduler.create_job(
            name=name,
            kind=kind,
            payload=payload,
            schedule=schedule,
            timezone_name=timezone_name,
            platform=context["platform"],
            chat_id=context["chat_id"],
            user_id=context["user_id"],
        )
        return _json({"ok": True, "job": _job_data(job)})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def schedule_reminder(
    name: str,
    schedule: str,
    text: str,
    timezone_name: str = _DEFAULT_TIMEZONE,
) -> str:
    """Create a durable reminder delivered directly to this authenticated chat.

    Args:
        name: Short human-readable reminder name.
        schedule: ISO timestamp with offset, five-field cron, or strict relative form like in 15m.
        text: Exact reminder text to send when due.
        timezone_name: IANA timezone used to evaluate cron expressions.
    """
    return _create(
        name=name,
        kind="reminder",
        payload=text,
        schedule=schedule,
        timezone_name=timezone_name,
    )


def schedule_agent_job(
    name: str,
    schedule: str,
    prompt: str,
    timezone_name: str = _DEFAULT_TIMEZONE,
) -> str:
    """Create a durable autonomous Kara job with a restricted read-only tool set.

    Args:
        name: Short human-readable job name.
        schedule: ISO timestamp with offset, five-field cron, or strict relative form like in 15m.
        prompt: Self-contained instructions for the future isolated agent run.
        timezone_name: IANA timezone used to evaluate cron expressions.
    """
    return _create(
        name=name,
        kind="agent",
        payload=prompt,
        schedule=schedule,
        timezone_name=timezone_name,
    )


def list_scheduled_jobs(include_finished: bool = False) -> str:
    """List scheduled jobs owned by the current authenticated user.

    Args:
        include_finished: Include completed and failed one-shot jobs.
    """
    try:
        context = _context()
        jobs = scheduler.list_jobs(
            user_id=context["user_id"], include_finished=include_finished
        )
        return _json({"ok": True, "jobs": [_job_data(job) for job in jobs]})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def pause_scheduled_job(job_id: str) -> str:
    """Pause one scheduled job owned by the authenticated user.

    Args:
        job_id: Scheduler job ID returned by list_scheduled_jobs.
    """
    try:
        context = _context()
        job = scheduler.pause_job(job_id, user_id=context["user_id"])
        return _json({"ok": True, "job": _job_data(job)})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def resume_scheduled_job(job_id: str) -> str:
    """Resume one scheduled job owned by the authenticated user.

    Args:
        job_id: Scheduler job ID returned by list_scheduled_jobs.
    """
    try:
        context = _context()
        job = scheduler.resume_job(job_id, user_id=context["user_id"])
        return _json({"ok": True, "job": _job_data(job)})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def delete_scheduled_job(job_id: str) -> str:
    """Permanently delete one scheduled job owned by the authenticated user.

    Args:
        job_id: Scheduler job ID returned by list_scheduled_jobs.
    """
    try:
        context = _context()
        scheduler.delete_job(job_id, user_id=context["user_id"])
        return _json({"ok": True, "deleted": job_id})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def run_scheduled_job_now(job_id: str) -> str:
    """Queue one owned job to run on the scheduler's next polling cycle.

    Agent jobs retain their restricted read-only tool policy when run manually.

    Args:
        job_id: Scheduler job ID returned by list_scheduled_jobs.
    """
    try:
        context = _context()
        job = scheduler.trigger_job(job_id, user_id=context["user_id"])
        return _json({"ok": True, "job": _job_data(job)})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
