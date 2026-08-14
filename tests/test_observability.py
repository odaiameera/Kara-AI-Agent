from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import kara
from memory import session_db
from gateway import commands as gw_commands
from providers.base import ChatResult, ToolCall, Usage
from tests.support import FakeProvider, make_session, tool_turn


class _DbSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            patch.object(config, "BRAIN_DIR", root),
            patch.object(session_db, "DB_PATH", root / "state.db"),
        ]
        for p in self._patches:
            p.start()
        session_db._initialized = False
        session_db.init_db()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        session_db._initialized = False
        self._tmp.cleanup()


class TurnTelemetryTests(_DbSandbox):
    def test_a_turn_records_tokens_and_timing(self) -> None:
        class _Reporting:
            id = "fake"

            def chat(self, model, messages, tools=None):
                return ChatResult(
                    content="done", usage=Usage(prompt_tokens=500, completion_tokens=25)
                )

        session = make_session(_Reporting())
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
        ):
            session.handle_message("hi")

        summary = session_db.usage_summary(session.session_key)
        self.assertEqual(summary["turns"], 1)
        self.assertEqual(summary["prompt_tokens"], 500)
        self.assertEqual(summary["completion_tokens"], 25)
        self.assertEqual(summary["total_tokens"], 525)

    def test_tool_calls_and_failures_are_counted(self) -> None:
        provider = FakeProvider(
            [
                tool_turn("read_file", {"path": "a"}),
                tool_turn("read_file", {"path": "b"}, call_id="call-2"),
                ChatResult(content="finished"),
            ]
        )
        session = make_session(provider)

        def boom(**kwargs):
            raise RuntimeError("disk on fire")

        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.dict(kara.TOOL_REGISTRY, {"read_file": boom}),
        ):
            session.handle_message("read things")

        summary = session_db.usage_summary(session.session_key)
        self.assertEqual(summary["tool_calls"], 2)
        self.assertEqual(summary["tool_errors"], 2)

    def test_usage_accumulates_across_turns(self) -> None:
        class _Reporting:
            id = "fake"

            def chat(self, model, messages, tools=None):
                return ChatResult(content="ok", usage=Usage(prompt_tokens=100))

        session = make_session(_Reporting())
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
        ):
            for _ in range(3):
                session.handle_message("hi")

        self.assertEqual(session_db.usage_summary(session.session_key)["turns"], 3)
        self.assertEqual(
            session_db.usage_summary(session.session_key)["prompt_tokens"], 300
        )

    def test_a_stopped_turn_records_its_reason(self) -> None:
        class _Looper:
            id = "fake"

            def chat(self, model, messages, tools=None):
                return tool_turn("read_file", {"path": "same"})

        session = make_session(_Looper())
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.dict(kara.TOOL_REGISTRY, {"read_file": lambda **k: "x"}),
        ):
            session.handle_message("go")

        with session_db._conn() as conn:
            row = conn.execute("SELECT finish_reason FROM turns").fetchone()
        self.assertEqual(row["finish_reason"], "repeated_tool_call")

    def test_usage_is_scoped_per_session(self) -> None:
        session_db.record_turn(
            "a", started_at="2026-01-01T00:00:00+00:00", duration_ms=1,
            provider_id="p", model="m", prompt_tokens=10,
        )
        session_db.record_turn(
            "b", started_at="2026-01-01T00:00:00+00:00", duration_ms=1,
            provider_id="p", model="m", prompt_tokens=90,
        )
        self.assertEqual(session_db.usage_summary("a")["prompt_tokens"], 10)
        self.assertEqual(session_db.usage_summary()["prompt_tokens"], 100)

    def test_usage_can_be_windowed_by_time(self) -> None:
        session_db.record_turn(
            "a", started_at="2020-01-01T00:00:00+00:00", duration_ms=1,
            provider_id="p", model="m", prompt_tokens=7,
        )
        session_db.record_turn(
            "a", started_at="2030-01-01T00:00:00+00:00", duration_ms=1,
            provider_id="p", model="m", prompt_tokens=11,
        )
        recent = session_db.usage_summary(since="2025-01-01T00:00:00+00:00")
        self.assertEqual(recent["prompt_tokens"], 11)

    def test_telemetry_failure_never_breaks_the_reply(self) -> None:
        session = make_session(FakeProvider([ChatResult(content="still fine")]))
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "record_turn", side_effect=RuntimeError("db down")),
        ):
            self.assertEqual(session.handle_message("hi"), "still fine")


class StructuredToolErrorTests(_DbSandbox):
    def _run_tool(self, fn):
        session = make_session(FakeProvider())
        with patch.dict(kara.TOOL_REGISTRY, {"read_file": fn}):
            failed = session._execute_tool_call(
                ToolCall(id="c1", name="read_file", arguments={}), None
            )
        return session.messages[-1], failed

    def test_a_raising_tool_is_marked_as_an_error(self) -> None:
        def boom(**kwargs):
            raise ValueError("nope")

        message, failed = self._run_tool(boom)
        self.assertTrue(failed)
        self.assertTrue(message["is_error"])
        self.assertTrue(message["content"].startswith(kara.TOOL_ERROR_PREFIX))
        self.assertIn("ValueError", message["content"])

    def test_a_successful_tool_is_not_marked(self) -> None:
        message, failed = self._run_tool(lambda **kw: "all good")
        self.assertFalse(failed)
        self.assertNotIn("is_error", message)
        self.assertEqual(message["content"], "all good")

    def test_a_result_mentioning_error_is_not_treated_as_a_failure(self) -> None:
        """The whole point: prose must not be mistaken for a failure."""
        message, failed = self._run_tool(lambda **kw: "Error codes found in the log: 3")
        self.assertFalse(failed)
        self.assertNotIn("is_error", message)

    def test_an_unknown_tool_is_an_error(self) -> None:
        session = make_session(FakeProvider())
        failed = session._execute_tool_call(
            ToolCall(id="c1", name="does_not_exist", arguments={}), None
        )
        self.assertTrue(failed)
        self.assertTrue(session.messages[-1]["is_error"])

    def test_a_blocked_tool_is_an_error(self) -> None:
        session = make_session(FakeProvider(), allowed_tool_names={"read_file"})
        failed = session._execute_tool_call(
            ToolCall(id="c1", name="write_file", arguments={}), None
        )
        self.assertTrue(failed)
        self.assertIn("not allowed", session.messages[-1]["content"])

    def test_the_error_flag_survives_a_reload(self) -> None:
        session_db.ensure_session("s", "cli", "p", "m")
        session_db.append_message(
            "s",
            {"role": "tool", "content": "boom", "tool_name": "t", "is_error": True},
        )
        restored = session_db.load_messages("s")
        self.assertTrue(restored[0]["is_error"])


class CommandTests(_DbSandbox):
    def _session(self):
        session = make_session(FakeProvider())
        session.provider.id = "fake"
        return session

    def test_usage_command_reports_both_scopes(self) -> None:
        session_db.record_turn(
            "cli:test", started_at="2026-01-01T00:00:00+00:00", duration_ms=1500,
            provider_id="p", model="m", prompt_tokens=1000, completion_tokens=50,
            tool_calls=2, tool_errors=1,
        )
        out = gw_commands.handle_command(self._session(), "/usage")
        self.assertIn("This session", out)
        self.assertIn("Today (all sessions)", out)
        self.assertIn("1,000", out)
        self.assertIn("1 failed", out)

    def test_usage_command_is_fine_with_no_data(self) -> None:
        self.assertIn("No turns recorded", gw_commands.handle_command(self._session(), "/usage"))

    def test_context_command_reports_occupancy(self) -> None:
        session = self._session()
        session.messages = [{"role": "system", "content": "x" * 4000}]
        out = gw_commands.handle_command(session, "/context")
        self.assertIn("Context:", out)
        self.assertIn("compaction starts at", out)
        self.assertIn("estimated", out)

    def test_context_prefers_the_provider_reported_count(self) -> None:
        session = self._session()
        session._last_prompt_tokens = 4321
        out = gw_commands.handle_command(session, "/context")
        self.assertIn("provider-reported", out)
        self.assertIn("4,321", out)

    def test_unknown_command_still_returns_none(self) -> None:
        self.assertIsNone(gw_commands.handle_command(self._session(), "hello there"))


if __name__ == "__main__":
    unittest.main()
