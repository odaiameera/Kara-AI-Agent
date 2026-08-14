from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class SchedulerStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        from scheduling import scheduler

        self.scheduler = scheduler
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "scheduler.db"
        self.db_patch = patch.object(scheduler, "DB_PATH", self.db_path)
        self.db_patch.start()
        scheduler._initialized = False

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_one_shot_reminder_is_persisted_and_claimed_when_due(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        job = self.scheduler.create_job(
            name="Call GP",
            kind="reminder",
            payload="Call the GP",
            schedule="2026-07-23T13:00:00+01:00",
            timezone_name="Europe/Dublin",
            platform="telegram",
            chat_id="123",
            user_id="456",
            now=now,
        )

        self.assertEqual(job.kind, "reminder")
        self.assertEqual(job.next_run_at, now)
        self.assertEqual(self.scheduler.claim_due_jobs(now=now - self.scheduler.ONE_SECOND), [])
        claimed = self.scheduler.claim_due_jobs(now=now)
        self.assertEqual([item.id for item in claimed], [job.id])
        self.assertEqual(claimed[0].status, "running")

        self.scheduler.complete_job(job.id, now=now)
        finished = self.scheduler.get_job(job.id)
        self.assertIsNotNone(finished)
        self.assertFalse(finished.enabled)
        self.assertEqual(finished.status, "completed")

    def test_cron_job_is_rescheduled_in_its_named_timezone(self) -> None:
        before_first = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)
        job = self.scheduler.create_job(
            name="Morning briefing",
            kind="agent",
            payload="Prepare the briefing",
            schedule="0 8 * * *",
            timezone_name="Europe/Dublin",
            platform="telegram",
            chat_id="123",
            user_id="456",
            now=before_first,
        )
        # Ireland is UTC+1 on 24 October, so local 08:00 is 07:00 UTC.
        self.assertEqual(
            job.next_run_at,
            datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc),
        )

        claimed = self.scheduler.claim_due_jobs(now=job.next_run_at)
        self.assertEqual(len(claimed), 1)
        self.scheduler.complete_job(job.id, now=job.next_run_at)
        rescheduled = self.scheduler.get_job(job.id)
        self.assertIsNotNone(rescheduled)
        self.assertTrue(rescheduled.enabled)
        self.assertEqual(rescheduled.status, "scheduled")
        # DST has ended by the next day, so local 08:00 is now 08:00 UTC.
        self.assertEqual(
            rescheduled.next_run_at,
            datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc),
        )

    def test_jobs_can_be_listed_paused_resumed_and_deleted_by_owner(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        job = self.scheduler.create_job(
            name="Hourly check",
            kind="agent",
            payload="Check status",
            schedule="0 * * * *",
            timezone_name="Europe/Dublin",
            platform="telegram",
            chat_id="123",
            user_id="456",
            now=now,
        )
        self.assertEqual([j.id for j in self.scheduler.list_jobs(user_id="456")], [job.id])
        self.assertEqual(self.scheduler.list_jobs(user_id="other"), [])

        paused = self.scheduler.pause_job(job.id, user_id="456")
        self.assertFalse(paused.enabled)
        self.assertEqual(paused.status, "paused")
        resumed = self.scheduler.resume_job(job.id, user_id="456", now=now)
        self.assertTrue(resumed.enabled)
        self.assertEqual(resumed.status, "scheduled")

        with self.assertRaises(PermissionError):
            self.scheduler.delete_job(job.id, user_id="other")
        self.scheduler.delete_job(job.id, user_id="456")
        self.assertIsNone(self.scheduler.get_job(job.id))

    def test_relative_schedule_and_run_now_are_supported(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        job = self.scheduler.create_job(
            name="Stretch",
            kind="reminder",
            payload="Time to stretch",
            schedule="in 15m",
            timezone_name="Europe/Dublin",
            platform="telegram",
            chat_id="123",
            user_id="456",
            now=now,
        )
        self.assertEqual(
            job.next_run_at,
            datetime(2026, 7, 23, 12, 15, tzinfo=timezone.utc),
        )
        triggered = self.scheduler.trigger_job(job.id, user_id="456", now=now)
        self.assertEqual(triggered.next_run_at, now)
        self.assertEqual([j.id for j in self.scheduler.claim_due_jobs(now=now)], [job.id])

    def test_gateway_restart_recovers_interrupted_running_jobs(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        job = self.scheduler.create_job(
            name="Recover me",
            kind="reminder",
            payload="Hello",
            schedule="2026-07-23T13:00:00+01:00",
            timezone_name="Europe/Dublin",
            platform="telegram",
            chat_id="123",
            user_id="456",
            now=now,
        )
        self.scheduler.claim_due_jobs(now=now)
        self.assertEqual(self.scheduler.get_job(job.id).status, "running")
        self.scheduler._initialized = False
        self.scheduler.init_db()
        self.assertEqual(self.scheduler.get_job(job.id).status, "scheduled")

    def test_obvious_secrets_are_rejected_before_persistence(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret"):
            self.scheduler.create_job(
                name="Unsafe",
                kind="agent",
                payload="Use API_KEY=super-secret-value tomorrow",
                schedule="in 1h",
                timezone_name="Europe/Dublin",
                platform="telegram",
                chat_id="123",
                user_id="456",
                now=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
