from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import scheduler
import scheduled_runner


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_message(self, *, chat_id, text: str) -> None:
        self.messages.append((str(chat_id), text))


class ScheduledRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(scheduler, "DB_PATH", Path(self.tmp.name) / "scheduler.db")
        self.db_patch.start()
        scheduler._initialized = False

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_reminder_is_delivered_without_starting_an_agent(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        job = scheduler.create_job(
            name="Medication",
            kind="reminder",
            payload="Take medication",
            schedule="2026-07-23T13:00:00+01:00",
            timezone_name="Europe/Dublin",
            platform="telegram",
            chat_id="123",
            user_id="456",
            now=now,
        )
        bot = _FakeBot()
        with patch.object(scheduled_runner, "KaraSession") as session_class:
            asyncio.run(scheduled_runner.run_due_jobs(bot, now=now))
        session_class.assert_not_called()
        self.assertEqual(bot.messages, [("123", "⏰ Medication\n\nTake medication")])
        self.assertEqual(scheduler.get_job(job.id).status, "completed")

    def test_agent_job_runs_in_fresh_session_with_read_only_tools(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        job = scheduler.create_job(
            name="Status report",
            kind="agent",
            payload="Inspect project status",
            schedule="2026-07-23T13:00:00+01:00",
            timezone_name="Europe/Dublin",
            platform="telegram",
            chat_id="123",
            user_id="456",
            now=now,
        )
        created: dict = {}

        class FakeSession:
            def __init__(self, session_key, channel, fresh, allowed_tool_names):
                created.update(
                    session_key=session_key,
                    channel=channel,
                    fresh=fresh,
                    allowed_tool_names=allowed_tool_names,
                )

            def handle_message(self, prompt: str) -> str:
                created["prompt"] = prompt
                return "Everything is healthy."

        bot = _FakeBot()
        with patch.object(scheduled_runner, "KaraSession", FakeSession):
            asyncio.run(scheduled_runner.run_due_jobs(bot, now=now))

        self.assertTrue(created["fresh"])
        self.assertEqual(created["channel"], "scheduled")
        self.assertIn("read_file", created["allowed_tool_names"])
        # Read-only document extraction, allowed alongside read_office_file.
        self.assertIn("read_pdf", created["allowed_tool_names"])
        self.assertIn("ocr_image", created["allowed_tool_names"])
        self.assertIn("web_search", created["allowed_tool_names"])
        self.assertNotIn("write_file", created["allowed_tool_names"])
        self.assertNotIn("computer_use", created["allowed_tool_names"])
        self.assertNotIn("email_send", created["allowed_tool_names"])
        self.assertNotIn("schedule_agent_job", created["allowed_tool_names"])
        self.assertEqual(created["prompt"], "Inspect project status")
        self.assertEqual(
            bot.messages,
            [("123", "🤖 Status report\n\nEverything is healthy.")],
        )
        self.assertEqual(scheduler.get_job(job.id).status, "completed")


if __name__ == "__main__":
    unittest.main()
