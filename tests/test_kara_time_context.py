from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import kara
from providers.base import ChatResult
from tests.support import make_session


class _CapturingProvider:
    def __init__(self) -> None:
        self.requests: list[list[dict]] = []

    def chat(self, model, messages, tools=None):
        self.requests.append(copy.deepcopy(messages))
        return ChatResult(content="ok")


class KaraRuntimeClockInjectionTests(unittest.TestCase):
    def test_clock_refreshes_for_every_request_without_mutating_history(self) -> None:
        provider = _CapturingProvider()
        session = make_session(provider)
        session.messages = [
            {"role": "system", "content": "BASE SYSTEM"},
            {"role": "user", "content": "hello"},
        ]

        with patch.object(
            kara.time_context,
            "build_runtime_time_context",
            side_effect=["CLOCK ONE", "CLOCK TWO"],
        ):
            session._chat(with_tools=False)
            session._chat(with_tools=False)

        # History is never mutated by the clock.
        self.assertEqual(session.messages[0]["content"], "BASE SYSTEM")

        # The clock is a trailing message, refreshed each request.
        self.assertEqual(provider.requests[0][-1]["content"], "CLOCK ONE")
        self.assertEqual(provider.requests[1][-1]["content"], "CLOCK TWO")
        self.assertTrue(provider.requests[0][-1]["ephemeral"])

    def test_the_cacheable_prefix_is_identical_between_requests(self) -> None:
        """The clock used to rewrite the system message on every single request."""
        provider = _CapturingProvider()
        session = make_session(provider)
        session.messages = [
            {"role": "system", "content": "BASE SYSTEM"},
            {"role": "user", "content": "hello"},
        ]

        with patch.object(
            kara.time_context,
            "build_runtime_time_context",
            side_effect=["CLOCK ONE", "CLOCK TWO"],
        ):
            session._chat(with_tools=False)
            session._chat(with_tools=False)

        first, second = provider.requests
        self.assertEqual(first[:-1], second[:-1], "prefix must be byte-identical")
        self.assertNotIn("CLOCK", first[0]["content"])


class SystemPromptTimezoneTests(unittest.TestCase):
    """The scheduling instruction used to hardcode the author's timezone while
    the runtime clock read KARA_TIMEZONE, so the two disagreed for everyone who
    configured one."""

    def test_scheduling_instruction_follows_the_configured_timezone(self) -> None:
        with patch.object(
            kara.time_context, "configured_timezone", return_value="America/New_York"
        ):
            prompt = kara.get_system_instruction()

        self.assertIn("America/New_York", prompt)
        self.assertNotIn("Europe/Dublin", prompt)

    def test_prompt_and_runtime_clock_name_the_same_zone(self) -> None:
        prompt = kara.get_system_instruction()
        clock = kara.time_context.build_runtime_time_context()
        zone = kara.time_context.configured_timezone()

        self.assertIn(zone, prompt)
        self.assertIn(f"Timezone: {zone}", clock)


if __name__ == "__main__":
    unittest.main()
