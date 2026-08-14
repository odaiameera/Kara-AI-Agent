from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scheduling import scheduler
from tools import scheduler_tools


class SchedulerToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(scheduler, "DB_PATH", Path(self.tmp.name) / "scheduler.db")
        self.db_patch.start()
        scheduler._initialized = False
        self.context_token = scheduler_tools.set_scheduler_request_context(
            platform="telegram", chat_id="123", user_id="456"
        )

    def tearDown(self) -> None:
        scheduler_tools.reset_scheduler_request_context(self.context_token)
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_reminder_tool_uses_bound_delivery_context_not_model_supplied_ids(self) -> None:
        with patch.object(scheduler, "_utc_now", return_value=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)):
            result = json.loads(
                scheduler_tools.schedule_reminder(
                    name="Medication",
                    schedule="2026-07-23T14:00:00+01:00",
                    text="Take medication",
                    timezone_name="Europe/Dublin",
                )
            )
        self.assertTrue(result["ok"])
        job = scheduler.get_job(result["job"]["id"])
        self.assertIsNotNone(job)
        self.assertEqual(job.chat_id, "123")
        self.assertEqual(job.user_id, "456")
        self.assertEqual(job.kind, "reminder")

    def test_scheduler_tools_refuse_calls_without_authenticated_context(self) -> None:
        scheduler_tools.reset_scheduler_request_context(self.context_token)
        result = json.loads(scheduler_tools.list_scheduled_jobs())
        self.assertFalse(result["ok"])
        self.assertIn("authenticated", result["error"])
        self.context_token = scheduler_tools.set_scheduler_request_context(
            platform="telegram", chat_id="123", user_id="456"
        )


if __name__ == "__main__":
    unittest.main()
