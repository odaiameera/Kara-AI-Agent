from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scheduling import time_context


class RuntimeTimeContextTests(unittest.TestCase):
    def test_context_contains_local_utc_day_offset_and_timezone(self) -> None:
        now = datetime(2026, 7, 23, 22, 45, 12, tzinfo=timezone.utc)
        text = time_context.build_runtime_time_context(
            timezone_name="Europe/Dublin", now=now
        )
        self.assertIn("2026-07-23T23:45+01:00", text)
        self.assertIn("2026-07-23T22:45+00:00", text)
        self.assertIn("Thursday", text)
        self.assertIn("Europe/Dublin", text)
        self.assertIn("+01:00", text)

    def test_the_clock_is_quantized_to_the_minute(self) -> None:
        """Identical within a minute, so a tool loop's requests share a prefix."""
        base = datetime(2026, 7, 23, 22, 45, 0, tzinfo=timezone.utc)
        texts = {
            time_context.build_runtime_time_context(
                timezone_name="Europe/Dublin",
                now=base.replace(second=second),
            )
            for second in (0, 17, 42, 59)
        }
        self.assertEqual(len(texts), 1)

    def test_a_new_minute_produces_a_new_clock(self) -> None:
        first = time_context.build_runtime_time_context(
            timezone_name="Europe/Dublin",
            now=datetime(2026, 7, 23, 22, 45, 0, tzinfo=timezone.utc),
        )
        second = time_context.build_runtime_time_context(
            timezone_name="Europe/Dublin",
            now=datetime(2026, 7, 23, 22, 46, 0, tzinfo=timezone.utc),
        )
        self.assertNotEqual(first, second)

    def test_naive_now_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            time_context.build_runtime_time_context(
                timezone_name="Europe/Dublin", now=datetime(2026, 7, 23, 12, 0)
            )


if __name__ == "__main__":
    unittest.main()
