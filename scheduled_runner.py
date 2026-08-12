"""Execute due Kara jobs and deliver their results through the gateway."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

import scheduler
from kara import KaraSession
from tools import registry

log = logging.getLogger("kara.scheduler.runner")
POLL_SECONDS = float(os.getenv("KARA_SCHEDULER_POLL_SECONDS", "15"))
TELEGRAM_LIMIT = 4096

# Autonomous runs are intentionally observational. They cannot write files, send
# mail, drive the desktop, execute tests, mutate memory, or schedule more jobs.
# Membership is declared per-module as SCHEDULED_SAFE and aggregated by the
# registry, so a tool cannot be added to this boundary from a distance.
SAFE_AGENT_TOOLS = registry.SCHEDULED_SAFE


def _render_job(job: scheduler.ScheduledJob) -> str:
    if job.kind == "reminder":
        return f"⏰ {job.name}\n\n{job.payload}"
    session = KaraSession(
        f"kara:scheduled:{job.id}:{int(job.next_run_at.timestamp())}",
        channel="scheduled",
        fresh=True,
        allowed_tool_names=SAFE_AGENT_TOOLS,
    )
    answer = session.handle_message(job.payload)
    return f"🤖 {job.name}\n\n{answer}"


def _split_message(text: str) -> list[str]:
    return [text[i : i + TELEGRAM_LIMIT] for i in range(0, len(text), TELEGRAM_LIMIT)] or [""]


async def run_due_jobs(bot: Any, *, now: datetime | None = None) -> int:
    """Claim, execute, deliver, and finalize all jobs currently due."""
    jobs = await asyncio.to_thread(scheduler.claim_due_jobs, now=now)
    completed = 0
    for job in jobs:
        try:
            text = await asyncio.to_thread(_render_job, job)
            for chunk in _split_message(text):
                await bot.send_message(chat_id=job.chat_id, text=chunk)
            await asyncio.to_thread(scheduler.complete_job, job.id, now=now)
            completed += 1
            log.info("Completed scheduled %s job %s", job.kind, job.id)
        except Exception as exc:
            log.exception("Scheduled job %s failed", job.id)
            try:
                await asyncio.to_thread(scheduler.fail_job, job.id, str(exc), now=now)
            except Exception:
                log.exception("Could not persist failure for scheduled job %s", job.id)
    return completed


async def scheduler_loop(bot: Any, *, poll_seconds: float = POLL_SECONDS) -> None:
    """Long-lived gateway loop that checks for due jobs without blocking polling."""
    while True:
        try:
            await run_due_jobs(bot)
        except Exception:
            log.exception("Scheduler polling iteration failed")
        await asyncio.sleep(poll_seconds)
