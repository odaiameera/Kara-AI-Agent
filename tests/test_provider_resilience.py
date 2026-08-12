from __future__ import annotations

import unittest
from unittest.mock import patch

import kara
from provider_base import (
    ChatResult,
    ProviderError,
    call_with_retry,
    is_retryable_status,
)
from tests.support import make_session


class RetryClassificationTests(unittest.TestCase):
    def test_rate_limit_and_gateway_errors_are_retryable(self) -> None:
        for status in (408, 429, 500, 502, 503, 504):
            self.assertTrue(is_retryable_status(status), status)

    def test_client_errors_are_not_retryable(self) -> None:
        for status in (400, 401, 403, 404, 422):
            self.assertFalse(is_retryable_status(status), status)

    def test_error_carries_its_status_and_retryability(self) -> None:
        err = ProviderError("boom", retryable=True, status_code=429)
        self.assertTrue(err.retryable)
        self.assertEqual(err.status_code, 429)

    def test_errors_default_to_not_retryable(self) -> None:
        self.assertFalse(ProviderError("boom").retryable)


class CallWithRetryTests(unittest.TestCase):
    def test_success_on_the_first_try_does_not_sleep(self) -> None:
        slept: list[float] = []
        result = call_with_retry(lambda: "ok", sleep=slept.append)
        self.assertEqual(result, "ok")
        self.assertEqual(slept, [])

    def test_transient_failures_are_retried_then_succeed(self) -> None:
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ProviderError("429", retryable=True, status_code=429)
            return "recovered"

        slept: list[float] = []
        self.assertEqual(
            call_with_retry(flaky, attempts=5, base_delay=0.01, sleep=slept.append),
            "recovered",
        )
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(slept), 2)

    def test_a_permanent_error_is_not_retried(self) -> None:
        attempts = {"n": 0}

        def bad():
            attempts["n"] += 1
            raise ProviderError("401", retryable=False, status_code=401)

        with self.assertRaises(ProviderError):
            call_with_retry(bad, attempts=5, base_delay=0.01, sleep=lambda _: None)
        self.assertEqual(attempts["n"], 1, "a 401 must not be retried")

    def test_retries_are_bounded(self) -> None:
        attempts = {"n": 0}

        def always_429():
            attempts["n"] += 1
            raise ProviderError("429", retryable=True, status_code=429)

        with self.assertRaises(ProviderError):
            call_with_retry(always_429, attempts=3, base_delay=0.01, sleep=lambda _: None)
        self.assertEqual(attempts["n"], 3)

    def test_backoff_grows(self) -> None:
        slept: list[float] = []

        def always():
            raise ProviderError("429", retryable=True, status_code=429)

        with self.assertRaises(ProviderError):
            call_with_retry(always, attempts=4, base_delay=1.0, sleep=slept.append)
        self.assertLess(slept[0], slept[-1])


class SessionResilienceTests(unittest.TestCase):
    def test_a_turn_survives_two_rate_limits(self) -> None:
        class _Flaky:
            id = "flaky"

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, model, messages, tools=None):
                self.calls += 1
                if self.calls <= 2:
                    raise ProviderError("429", retryable=True, status_code=429)
                return ChatResult(content="finally")

        provider = _Flaky()
        session = make_session(provider)
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.config, "PROVIDER_RETRY_BASE_DELAY", 0.001),
            patch.object(kara.config, "PROVIDER_RETRY_ATTEMPTS", 5),
        ):
            self.assertEqual(session.handle_message("hi"), "finally")
        self.assertEqual(provider.calls, 3)

    def test_permanent_failure_rolls_the_turn_back(self) -> None:
        class _Dead:
            def chat(self, model, messages, tools=None):
                raise ProviderError("401 unauthorized", status_code=401)

        session = make_session(_Dead())
        session.messages = [{"role": "system", "content": "SYS"}]
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "replace_messages") as replace,
            self.assertRaises(ProviderError),
        ):
            session.handle_message("hello")

        # Only the pre-turn history survives — no dangling user message.
        self.assertEqual(session.messages, [{"role": "system", "content": "SYS"}])
        replace.assert_called_once()

    def test_rollback_leaves_earlier_turns_alone(self) -> None:
        class _Dead:
            def chat(self, model, messages, tools=None):
                raise ProviderError("nope")

        session = make_session(_Dead())
        session.messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        before = list(session.messages)
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "replace_messages"),
            self.assertRaises(ProviderError),
        ):
            session.handle_message("new question")
        self.assertEqual(session.messages, before)

    def test_exhausted_retries_fail_over_to_another_provider(self) -> None:
        class _Down:
            id = "down"

            def chat(self, model, messages, tools=None):
                raise ProviderError("503", retryable=True, status_code=503)

        class _Up:
            id = "up"

            def chat(self, model, messages, tools=None):
                return ChatResult(content="served by backup")

        session = make_session(_Down())
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "update_session_model"),
            patch.object(kara.models, "set_active"),
            patch.object(kara.providers, "first_reachable_provider", return_value=_Up()),
            patch.object(kara.config, "PROVIDER_RETRY_BASE_DELAY", 0.001),
            patch.object(kara.config, "PROVIDER_RETRY_ATTEMPTS", 2),
        ):
            reply = session.handle_message("hi")
        self.assertEqual(reply, "served by backup")
        self.assertEqual(session.provider.id, "up")

    def test_no_failover_when_the_error_is_permanent(self) -> None:
        class _Bad:
            id = "bad"

            def chat(self, model, messages, tools=None):
                raise ProviderError("400 bad request", status_code=400)

        session = make_session(_Bad())
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "replace_messages"),
            patch.object(kara.providers, "first_reachable_provider") as fallback,
            self.assertRaises(ProviderError),
        ):
            session.handle_message("hi")
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
