import json
import unittest
from unittest import mock

from tools import buzz_tools


class BuzzToolsTests(unittest.TestCase):
    def setUp(self):
        self.context_token = buzz_tools.set_buzz_request_context(
            channel="19741b6e-85d1-5cd7-a593-fa0cf9392edf",
            reply_to="07640c5a01a2231b89c6948c0cf70dc6656166bd79c4c6a76b80454bfe6e1c23",
        )

    def tearDown(self):
        buzz_tools.reset_buzz_request_context(self.context_token)

    @mock.patch.dict(
        "os.environ",
        {"BUZZ_RELAY_URL": "wss://relay", "BUZZ_PRIVATE_KEY": "secret", "BUZZ_AUTH_TAG": "tag"},
        clear=True,
    )
    @mock.patch("tools.buzz_tools.subprocess.run")
    def test_threaded_send_uses_argv_and_returns_event_id(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"accepted": True, "event_id": "abc"})
        run.return_value.stderr = ""
        result = json.loads(buzz_tools.buzz_send_message("hello"))
        self.assertEqual(result, {"ok": True, "accepted": True, "event_id": "abc"})
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "buzz", "messages", "send", "--channel",
                "19741b6e-85d1-5cd7-a593-fa0cf9392edf", "--content", "hello",
                "--reply-to",
                "07640c5a01a2231b89c6948c0cf70dc6656166bd79c4c6a76b80454bfe6e1c23",
            ],
        )
        self.assertNotIn("secret", command)
        self.assertTrue(buzz_tools.buzz_message_was_published())

    @mock.patch.dict(
        "os.environ",
        {"BUZZ_RELAY_URL": "wss://relay", "BUZZ_PRIVATE_KEY": "secret", "BUZZ_AUTH_TAG": "tag"},
        clear=True,
    )
    @mock.patch("tools.buzz_tools.subprocess.run")
    def test_second_send_in_one_turn_is_suppressed(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"accepted": True, "event_id": "first-event"})
        run.return_value.stderr = ""

        first = json.loads(buzz_tools.buzz_send_message("finished answer"))
        second = json.loads(buzz_tools.buzz_send_message("accidental duplicate"))

        self.assertTrue(first["ok"])
        self.assertEqual(
            second,
            {
                "ok": True,
                "accepted": True,
                "event_id": "first-event",
                "duplicate_suppressed": True,
            },
        )
        run.assert_called_once()

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_missing_credentials_does_not_spawn(self):
        result = json.loads(buzz_tools.buzz_send_message("hello"))
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["missing"],
            ["BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG"],
        )

    @mock.patch.dict(
        "os.environ",
        {"BUZZ_RELAY_URL": "wss://relay", "BUZZ_PRIVATE_KEY": "secret", "BUZZ_AUTH_TAG": "tag"},
        clear=True,
    )
    @mock.patch("tools.buzz_tools.subprocess.run")
    def test_unstructured_failure_is_not_echoed(self, run):
        run.return_value.returncode = 2
        run.return_value.stdout = ""
        run.return_value.stderr = "unexpected secret diagnostic"
        result = json.loads(buzz_tools.buzz_send_message("hello"))
        self.assertEqual(result["error"], "buzz CLI failed without structured JSON")
        self.assertNotIn("secret diagnostic", json.dumps(result))

    @mock.patch.dict(
        "os.environ",
        {"BUZZ_RELAY_URL": "wss://relay", "BUZZ_PRIVATE_KEY": "secret", "BUZZ_AUTH_TAG": "tag"},
        clear=True,
    )
    @mock.patch("tools.buzz_tools.subprocess.run")
    def test_send_refuses_model_call_without_bound_route(self, run):
        buzz_tools.reset_buzz_request_context(self.context_token)
        result = json.loads(buzz_tools.buzz_send_message("hello"))
        self.assertFalse(result["ok"])
        self.assertIn("bound Buzz request", result["error"])
        run.assert_not_called()
        self.context_token = buzz_tools.set_buzz_request_context(
            channel="19741b6e-85d1-5cd7-a593-fa0cf9392edf",
            reply_to="07640c5a01a2231b89c6948c0cf70dc6656166bd79c4c6a76b80454bfe6e1c23",
        )


if __name__ == "__main__":
    unittest.main()
