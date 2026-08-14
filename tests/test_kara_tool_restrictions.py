from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import kara
from providers.base import ChatResult
from tests.support import FakeProvider as _FakeProvider
from tests.support import make_session, tool_turn as _tool_turn
from tools import registry


class KaraToolRestrictionTests(unittest.TestCase):
    def _session(self, provider: _FakeProvider, allowed: set[str]):
        return make_session(
            provider,
            session_key="scheduled:test",
            channel="scheduled",
            allowed_tool_names=allowed,
        )

    def test_chat_exposes_only_explicitly_allowed_tools(self) -> None:
        provider = _FakeProvider()
        session = self._session(provider, {"read_file", "web_search"})
        session._chat(with_tools=True)
        names = {item["function"]["name"] for item in provider.tool_batches[0]}
        self.assertEqual(names, {"read_file", "web_search"})

    def test_hallucinated_disallowed_tool_is_not_executed(self) -> None:
        provider = _FakeProvider(
            [
                _tool_turn("write_file", {"path": "x", "content": "bad"}),
                ChatResult(content="Handled safely."),
            ]
        )
        session = self._session(provider, {"read_file"})
        write_mock = Mock(return_value="should not happen")
        with (
            patch.dict(kara.TOOL_REGISTRY, {"write_file": write_mock}),
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
        ):
            reply = session.handle_message("Inspect only")
        self.assertEqual(reply, "Handled safely.")
        write_mock.assert_not_called()
        tool_messages = [m for m in session.messages if m.get("role") == "tool"]
        self.assertIn("not allowed", tool_messages[0]["content"])

    def test_restricted_session_ignores_group_activation(self) -> None:
        """Gating must never widen a restricted session past its allowlist."""
        session = self._session(_FakeProvider(), {"read_file"})
        session._activate_tool_group(group="github")
        names = {item["function"]["name"] for item in session._visible_schemas()}
        self.assertEqual(names, {"read_file"})

    def test_restricted_session_cannot_activate_its_way_to_a_write_tool(self) -> None:
        provider = _FakeProvider(
            [
                _tool_turn(registry.ACTIVATE_TOOL, {"group": "file"}),
                ChatResult(content="Done."),
            ]
        )
        session = self._session(provider, {"read_file"})
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
        ):
            session.handle_message("load the file tools")
        # The activation call is rejected by the allowlist before it is handled,
        # and the second request still only carries the allowed tool.
        names = {item["function"]["name"] for item in provider.tool_batches[-1]}
        self.assertEqual(names, {"read_file"})


class KaraToolGatingTests(unittest.TestCase):
    def _session(self, provider: _FakeProvider):
        return make_session(provider)

    def _run(self, session, text: str) -> None:
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
        ):
            session.handle_message(text)

    def test_plain_message_exposes_only_always_on_groups(self) -> None:
        provider = _FakeProvider()
        session = self._session(provider)
        self._run(session, "how are you today?")
        names = {item["function"]["name"] for item in provider.tool_batches[0]}
        expected = {
            name for g in registry.ALWAYS_ON for name in registry.GROUPS[g]
        } | {registry.ACTIVATE_TOOL}
        self.assertEqual(names, expected)
        self.assertNotIn("github_status", names)
        self.assertNotIn("email_send", names)

    def test_keyword_in_message_pre_activates_the_group(self) -> None:
        provider = _FakeProvider()
        session = self._session(provider)
        self._run(session, "list the open pull requests on my repo")
        names = {item["function"]["name"] for item in provider.tool_batches[0]}
        self.assertIn("github_list_pull_requests", names)

    def test_model_can_activate_a_group_the_keywords_missed(self) -> None:
        provider = _FakeProvider(
            [
                _tool_turn(registry.ACTIVATE_TOOL, {"group": "email"}),
                ChatResult(content="Done."),
            ]
        )
        session = self._session(provider)
        self._run(session, "anything new for me?")

        first = {item["function"]["name"] for item in provider.tool_batches[0]}
        self.assertNotIn("email_read", first)

        second = {item["function"]["name"] for item in provider.tool_batches[1]}
        self.assertIn("email_read", second)
        self.assertIn("email", session.active_groups)

    def test_activating_an_unknown_group_reports_the_valid_ones(self) -> None:
        session = self._session(_FakeProvider())
        result = session._activate_tool_group(group="nonsense")
        self.assertIn("unknown tool group", result)
        self.assertIn("github", result)
        self.assertEqual(session.active_groups, set(registry.ALWAYS_ON))

    def test_activation_persists_for_the_rest_of_the_session(self) -> None:
        provider = _FakeProvider()
        session = self._session(provider)
        self._run(session, "check my github notifications")
        self._run(session, "and what about that?")
        names = {item["function"]["name"] for item in provider.tool_batches[-1]}
        self.assertIn("github_list_notifications", names)


if __name__ == "__main__":
    unittest.main()
