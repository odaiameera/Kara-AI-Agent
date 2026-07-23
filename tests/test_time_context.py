from __future__ import annotations

import unittest
from datetime import datetime, timezone

import time_context


class RuntimeTimeContextTests(unittest.TestCase):
    def test_context_contains_local_utc_day_offset_and_timezone(self) -> None:
        now = datetime(2026, 7, 23, 22, 45, 12, tzinfo=timezone.utc)
        text = time_context.build_runtime_time_context(
            timezone_name="Europe/Dublin", now=now
        )
        self.assertIn("2026-07-23T23:45:12+01:00", text)
        self.assertIn("2026-07-23T22:45:12+00:00", text)
        self.assertIn("Thursday", text)
        self.assertIn("Europe/Dublin", text)
        self.assertIn("+01:00", text)

    def test_naive_now_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            time_context.build_runtime_time_context(
                timezone_name="Europe/Dublin", now=datetime(2026, 7, 23, 12, 0)
            )


if __name__ == "__main__":
    unittest.main()
