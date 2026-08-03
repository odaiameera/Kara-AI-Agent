import unittest
from unittest import mock

import kara


class BuzzToolRegistrationTests(unittest.TestCase):
    def test_buzz_tool_is_registered(self):
        self.assertIn("buzz_send_message", kara.TOOL_REGISTRY)
        schema = next(
            item for item in kara.TOOL_SCHEMAS
            if item["function"]["name"] == "buzz_send_message"
        )
        self.assertEqual(
            set(schema["function"]["parameters"]["properties"]), {"content"}
        )

    def test_non_buzz_session_does_not_offer_buzz_tool(self):
        session = object.__new__(kara.KaraSession)
        session.channel = "telegram"
        session.allowed_tool_names = None
        session.messages = [{"role": "system", "content": "test"}]
        session.model = "test-model"
        session.provider = mock.Mock()
        session.provider.chat.return_value = {"message": {"content": "ok"}}
        session._chat()
        tool_names = {
            item["function"]["name"]
            for item in session.provider.chat.call_args.kwargs["tools"]
        }
        self.assertNotIn("buzz_send_message", tool_names)


if __name__ == "__main__":
    unittest.main()
