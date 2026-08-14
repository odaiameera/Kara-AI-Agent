from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scheduling import scheduler
from gateway.platforms import telegram as telegram_adapter
from tools import scheduler_tools


class GatewaySchedulerContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(scheduler, "DB_PATH", Path(self.tmp.name) / "scheduler.db")
        self.db_patch.start()
        scheduler._initialized = False

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_chat_reply_binds_and_then_clears_authenticated_scheduler_context(self) -> None:
        class FakeSession:
            def handle_message(self, _text: str) -> str:
                return scheduler_tools.schedule_reminder(
                    "Test", "2099-01-01T09:00:00+00:00", "Hello"
                )

        with (
            patch.object(telegram_adapter.gw_sessions, "get_session", return_value=FakeSession()),
            patch.object(telegram_adapter.gw_commands, "handle_command", return_value=None),
        ):
            response, is_rich = telegram_adapter._chat_reply(456, 123, "remind me")

        payload = json.loads(response)
        self.assertTrue(payload["ok"])
        job = scheduler.get_job(payload["job"]["id"])
        self.assertEqual(job.chat_id, "123")
        self.assertEqual(job.user_id, "456")
        self.assertTrue(is_rich)

        after = json.loads(scheduler_tools.list_scheduled_jobs())
        self.assertFalse(after["ok"])


if __name__ == "__main__":
    unittest.main()
