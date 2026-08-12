from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import kara
from provider_base import ChatResult
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

        self.assertEqual(session.messages[0]["content"], "BASE SYSTEM")
        self.assertEqual(
            provider.requests[0][0]["content"], "BASE SYSTEM\n\nCLOCK ONE"
        )
        self.assertEqual(
            provider.requests[1][0]["content"], "BASE SYSTEM\n\nCLOCK TWO"
        )
        self.assertNotIn("CLOCK ONE", provider.requests[1][0]["content"])


if __name__ == "__main__":
    unittest.main()
