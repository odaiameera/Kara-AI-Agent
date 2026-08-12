from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import kara
from kara import TurnStopped, _TurnBudget
from provider_base import ChatResult, ToolCall
from tests.support import make_session


class _LoopingProvider:
    """A model that never stops asking for the same tool."""

    def __init__(self, *, vary: bool = False) -> None:
        self.calls = 0
        self.vary = vary

    def chat(self, model, messages, tools=None):
        self.calls += 1
        args = {"path": f"file{self.calls}.txt"} if self.vary else {"path": "a.txt"}
        return ChatResult(
            tool_calls=(ToolCall(id=f"c{self.calls}", name="read_file", arguments=args),),
            finish_reason="tool_calls",
        )


def _session(provider, **overrides):
    session = make_session(provider)
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def _run(session, text="go"):
    with (
        patch.object(kara, "set_computer_request_context"),
        patch.object(kara.session_db, "clear_interrupted"),
        patch.dict(kara.TOOL_REGISTRY, {"read_file": lambda **kw: "contents"}),
    ):
        return session.handle_message(text)


class BudgetUnitTests(unittest.TestCase):
    def test_iteration_cap_raises(self) -> None:
        budget = _TurnBudget(max_iterations=2)
        budget.start_iteration(None)
        budget.start_iteration(None)
        with self.assertRaises(TurnStopped) as ctx:
            budget.start_iteration("read_file")
        self.assertEqual(ctx.exception.reason, "max_iterations")

    def test_timeout_raises(self) -> None:
        budget = _TurnBudget(timeout_seconds=0)
        budget.started -= 5
        with self.assertRaises(TurnStopped) as ctx:
            budget.start_iteration(None)
        self.assertEqual(ctx.exception.reason, "timeout")

    def test_identical_calls_trip_the_repeat_guard(self) -> None:
        budget = _TurnBudget(max_repeats=3)
        budget.record_call("read_file", {"path": "a"})
        budget.record_call("read_file", {"path": "a"})
        with self.assertRaises(TurnStopped) as ctx:
            budget.record_call("read_file", {"path": "a"})
        self.assertEqual(ctx.exception.reason, "repeated_tool_call")

    def test_differing_arguments_reset_the_repeat_guard(self) -> None:
        budget = _TurnBudget(max_repeats=3)
        for i in range(10):
            budget.record_call("read_file", {"path": f"file{i}"})

    def test_argument_order_does_not_defeat_the_guard(self) -> None:
        budget = _TurnBudget(max_repeats=2)
        budget.record_call("t", {"a": 1, "b": 2})
        with self.assertRaises(TurnStopped):
            budget.record_call("t", {"b": 2, "a": 1})

    def test_unserializable_arguments_do_not_crash(self) -> None:
        budget = _TurnBudget(max_repeats=99)
        budget.record_call("t", {"fn": object()})


class LoopTerminationTests(unittest.TestCase):
    def test_runaway_loop_terminates_at_the_iteration_cap(self) -> None:
        provider = _LoopingProvider(vary=True)
        session = _session(provider)
        with patch.object(kara.config, "MAX_TOOL_ITERATIONS", 5):
            reply = _run(session)
        self.assertLessEqual(provider.calls, 5)
        self.assertIn("stopped", reply.lower())

    def test_repeated_identical_call_terminates_sooner(self) -> None:
        provider = _LoopingProvider(vary=False)
        session = _session(provider)
        with patch.object(kara.config, "MAX_TOOL_ITERATIONS", 100):
            reply = _run(session)
        self.assertLess(provider.calls, 100)
        self.assertIn("same arguments", reply)

    def test_stopped_turn_leaves_a_note_in_history(self) -> None:
        session = _session(_LoopingProvider(vary=True))
        with patch.object(kara.config, "MAX_TOOL_ITERATIONS", 3):
            _run(session)
        last = session.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertIn("turn stopped", last["content"])

    def test_normal_turn_is_unaffected(self) -> None:
        class _Quick:
            def chat(self, model, messages, tools=None):
                return ChatResult(content="All done.")

        self.assertEqual(_run(_session(_Quick())), "All done.")


class CancellationTests(unittest.TestCase):
    def test_a_stale_stop_does_not_kill_a_fresh_turn(self) -> None:
        """A stop applies to the turn it interrupted, not the next one."""

        class _Quick:
            def chat(self, model, messages, tools=None):
                return ChatResult(content="fine")

        session = _session(_Quick())
        session.request_stop()  # nothing running; this must not carry over
        self.assertEqual(_run(session), "fine")
        self.assertFalse(session.stop_requested)

    def test_stop_mid_loop_ends_the_turn(self) -> None:
        class _StopsItself:
            def __init__(self) -> None:
                self.calls = 0
                self.session = None

            def chat(self, model, messages, tools=None):
                self.calls += 1
                if self.calls == 2:
                    self.session.request_stop()
                return ChatResult(
                    tool_calls=(ToolCall(id="c", name="read_file", arguments={"path": f"{self.calls}"}),),
                    finish_reason="tool_calls",
                )

        provider = _StopsItself()
        session = _session(provider)
        provider.session = session
        reply = _run(session)
        self.assertIn("Stopped at your request", reply)
        self.assertLessEqual(provider.calls, 3)

    def test_a_cancelled_turn_does_not_disable_the_next_one(self) -> None:
        class _StopsThenBehaves:
            def __init__(self) -> None:
                self.calls = 0
                self.session = None

            def chat(self, model, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    self.session.request_stop()
                    return ChatResult(
                        tool_calls=(
                            ToolCall(id="c", name="read_file", arguments={"path": "a"}),
                        ),
                        finish_reason="tool_calls",
                    )
                return ChatResult(content="fine")

        provider = _StopsThenBehaves()
        session = _session(provider)
        provider.session = session
        self.assertIn("Stopped at your request", _run(session))
        # The next message must run normally.
        self.assertEqual(_run(session), "fine")

    def test_stop_requested_reflects_state(self) -> None:
        session = _session(_LoopingProvider())
        self.assertFalse(session.stop_requested)
        session.request_stop()
        self.assertTrue(session.stop_requested)


class GatewayStopWiringTests(unittest.TestCase):
    def test_request_stop_reaches_an_active_session(self) -> None:
        from gateway import sessions as gw_sessions

        session = _session(_LoopingProvider())
        gw_sessions._active["k"] = session
        try:
            self.assertTrue(gw_sessions.request_stop("k"))
            self.assertTrue(session.stop_requested)
        finally:
            gw_sessions._active.pop("k", None)

    def test_request_stop_on_an_unknown_session_is_false(self) -> None:
        from gateway import sessions as gw_sessions

        self.assertFalse(gw_sessions.request_stop("nope"))

    def test_turn_lock_is_stable_per_key(self) -> None:
        from gateway import sessions as gw_sessions

        self.assertIs(gw_sessions.turn_lock("a"), gw_sessions.turn_lock("a"))
        self.assertIsNot(gw_sessions.turn_lock("a"), gw_sessions.turn_lock("b"))


if __name__ == "__main__":
    unittest.main()
