from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import kara
from providers.base import ChatResult, ToolCall
from tests.support import FakeProvider, make_session
from tools import registry


def _batch(*names: str) -> ChatResult:
    return ChatResult(
        tool_calls=tuple(
            ToolCall(id=f"c{i}", name=name, arguments={"n": i})
            for i, name in enumerate(names)
        ),
        finish_reason="tool_calls",
    )


def _run(session, registry_patch: dict, text: str = "go"):
    with (
        patch.object(kara, "set_computer_request_context"),
        patch.object(kara.session_db, "clear_interrupted"),
        patch.object(kara.session_db, "record_turn"),
        patch.dict(kara.TOOL_REGISTRY, registry_patch),
    ):
        return session.handle_message(text)


class ReadOnlyDeclarationTests(unittest.TestCase):
    def test_read_only_is_a_superset_of_scheduled_safe(self) -> None:
        self.assertTrue(set(registry.SCHEDULED_SAFE).issubset(registry.READ_ONLY))

    def test_known_reads_are_read_only(self) -> None:
        for name in (
            "read_file",
            "web_search",
            "github_get_pull_request",
            "email_read",
            "query_sqlite_database",
        ):
            self.assertTrue(registry.is_read_only(name), name)

    def test_writes_and_side_effects_are_not_read_only(self) -> None:
        for name in (
            "write_file",
            "move_file",
            "email_send",
            "computer_use",
            "run_python_tests",
            "git_push_changes",
            "github_create_pull_request",
            "core_memory_append",
            "save_learning",
            "schedule_agent_job",
            "write_obsidian_note",
            "mnemosyne_call_tool",
            "git_clone_repository",
        ):
            self.assertFalse(registry.is_read_only(name), name)

    def test_activate_tool_group_is_not_read_only(self) -> None:
        """It mutates session state, so it must never run concurrently."""
        self.assertFalse(registry.is_read_only(registry.ACTIVATE_TOOL))

    def test_every_read_only_name_is_registered(self) -> None:
        self.assertTrue(set(registry.READ_ONLY).issubset(registry.TOOL_REGISTRY))


class ParallelExecutionTests(unittest.TestCase):
    def test_a_read_only_batch_runs_concurrently(self) -> None:
        started = threading.Barrier(3, timeout=5)

        def blocking(**kwargs):
            # Only completes if three calls are in flight at once.
            started.wait()
            return "done"

        session = make_session(
            FakeProvider([_batch("read_file", "web_search", "file_info"), ChatResult(content="ok")])
        )
        reply = _run(
            session,
            {"read_file": blocking, "web_search": blocking, "file_info": blocking},
        )
        self.assertEqual(reply, "ok")

    def test_results_keep_request_order_regardless_of_completion(self) -> None:
        def slow(**kwargs):
            time.sleep(0.05)
            return "slow result"

        def fast(**kwargs):
            return "fast result"

        session = make_session(
            FakeProvider([_batch("read_file", "web_search"), ChatResult(content="ok")])
        )
        _run(session, {"read_file": slow, "web_search": fast})

        tool_messages = [m for m in session.messages if m["role"] == "tool"]
        self.assertEqual(
            [m["tool_name"] for m in tool_messages], ["read_file", "web_search"]
        )
        self.assertEqual(tool_messages[0]["content"], "slow result")
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["c0", "c1"])

    def test_a_batch_containing_a_write_runs_sequentially(self) -> None:
        order: list[str] = []

        def track(name):
            def fn(**kwargs):
                order.append(f"start:{name}")
                time.sleep(0.02)
                order.append(f"end:{name}")
                return name

            return fn

        session = make_session(
            FakeProvider([_batch("read_file", "write_file"), ChatResult(content="ok")])
        )
        _run(session, {"read_file": track("read"), "write_file": track("write")})

        # Sequential execution never interleaves start/end pairs.
        self.assertEqual(
            order, ["start:read", "end:read", "start:write", "end:write"]
        )

    def test_a_single_call_is_not_parallelised(self) -> None:
        session = make_session(
            FakeProvider([_batch("read_file"), ChatResult(content="ok")])
        )
        with patch.object(kara, "ThreadPoolExecutor") as pool:
            _run(session, {"read_file": lambda **k: "x"})
        pool.assert_not_called()

    def test_failures_in_a_parallel_batch_are_marked_and_counted(self) -> None:
        def boom(**kwargs):
            raise RuntimeError("nope")

        session = make_session(
            FakeProvider([_batch("read_file", "web_search"), ChatResult(content="ok")])
        )
        _run(session, {"read_file": boom, "web_search": lambda **k: "fine"})

        tool_messages = [m for m in session.messages if m["role"] == "tool"]
        self.assertTrue(tool_messages[0].get("is_error"))
        self.assertIn("RuntimeError", tool_messages[0]["content"])
        self.assertFalse(tool_messages[1].get("is_error"))

    def test_one_failure_does_not_lose_the_other_results(self) -> None:
        def boom(**kwargs):
            raise RuntimeError("nope")

        session = make_session(
            FakeProvider(
                [_batch("read_file", "web_search", "file_info"), ChatResult(content="ok")]
            )
        )
        _run(
            session,
            {
                "read_file": boom,
                "web_search": lambda **k: "a",
                "file_info": lambda **k: "b",
            },
        )
        tool_messages = [m for m in session.messages if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 3)

    def test_every_call_gets_a_result_message(self) -> None:
        session = make_session(
            FakeProvider(
                [_batch("read_file", "web_search", "file_info", "web_fetch"),
                 ChatResult(content="ok")]
            )
        )
        _run(session, {n: (lambda **k: "r") for n in
                       ("read_file", "web_search", "file_info", "web_fetch")})
        tool_messages = [m for m in session.messages if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 4)
        self.assertEqual(
            [m["tool_call_id"] for m in tool_messages], ["c0", "c1", "c2", "c3"]
        )

    def test_the_tool_callback_fires_for_every_parallel_call(self) -> None:
        seen: list[str] = []
        session = make_session(
            FakeProvider([_batch("read_file", "web_search"), ChatResult(content="ok")])
        )
        with (
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "record_turn"),
            patch.dict(
                kara.TOOL_REGISTRY,
                {"read_file": lambda **k: "a", "web_search": lambda **k: "b"},
            ),
        ):
            session.handle_message("go", on_tool_call=lambda n, a: seen.append(n))
        self.assertEqual(seen, ["read_file", "web_search"])


if __name__ == "__main__":
    unittest.main()
