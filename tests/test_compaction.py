from __future__ import annotations

import unittest
from unittest.mock import patch

import context_budget
import kara
from provider_base import ChatResult
from tests.support import FakeProvider, make_session


def _exchange(index: int, *, tool_result_chars: int = 200) -> list[dict]:
    """One user turn answered via a tool call — the shape that must stay intact."""
    return [
        {"role": "user", "content": f"question {index}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                }
            ],
        },
        {
            "role": "tool",
            "content": "x" * tool_result_chars,
            "tool_name": "read_file",
            "tool_call_id": f"call-{index}",
        },
        {"role": "assistant", "content": f"answer {index}"},
    ]


def _history(count: int, **kwargs) -> list[dict]:
    messages = [{"role": "system", "content": "SYSTEM PROMPT"}]
    for i in range(count):
        messages.extend(_exchange(i, **kwargs))
    return messages


def _pairs_are_intact(messages: list[dict]) -> bool:
    """Every tool result must follow an assistant turn that requested it."""
    open_ids: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            open_ids = {c["id"] for c in message["tool_calls"]}
        elif message.get("role") == "tool":
            if message.get("tool_call_id") not in open_ids:
                return False
    return True


class GroupingTests(unittest.TestCase):
    def test_tool_calls_group_with_their_results(self) -> None:
        units = context_budget.group_units(_exchange(0))
        # user | (assistant+tool) | assistant
        self.assertEqual([len(u.messages) for u in units], [1, 2, 1])

    def test_multiple_results_attach_to_one_assistant_turn(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "t1"}},
                    {"id": "b", "type": "function", "function": {"name": "t2"}},
                ],
            },
            {"role": "tool", "content": "r1", "tool_call_id": "a", "tool_name": "t1"},
            {"role": "tool", "content": "r2", "tool_call_id": "b", "tool_name": "t2"},
        ]
        units = context_budget.group_units(messages)
        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0].messages), 3)

    def test_plain_messages_are_their_own_units(self) -> None:
        units = context_budget.group_units(
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        )
        self.assertEqual(len(units), 2)


class CompactionTests(unittest.TestCase):
    def test_history_under_budget_is_untouched(self) -> None:
        messages = _history(2)
        out, report = context_budget.compact_messages(messages, limit_tokens=100_000)
        self.assertFalse(report.changed)
        self.assertEqual(out, messages)

    def test_oversized_history_lands_under_budget(self) -> None:
        messages = _history(40, tool_result_chars=4000)
        out, report = context_budget.compact_messages(messages, limit_tokens=3000)
        self.assertTrue(report.changed)
        self.assertLess(report.tokens_after, report.tokens_before)
        self.assertLessEqual(context_budget.estimate_messages_tokens(out), 3000)

    def test_tool_call_pairs_survive_compaction(self) -> None:
        """The invariant Codex's Responses replay depends on."""
        messages = _history(40, tool_result_chars=4000)
        out, _ = context_budget.compact_messages(messages, limit_tokens=3000)
        self.assertTrue(_pairs_are_intact(out))

    def test_no_orphan_tool_message_leads_the_history(self) -> None:
        messages = _history(30, tool_result_chars=4000)
        out, _ = context_budget.compact_messages(messages, limit_tokens=2000)
        after_system = [m for m in out if m.get("role") != "system"]
        if after_system:
            self.assertNotEqual(after_system[0].get("role"), "tool")

    def test_system_prompt_is_always_kept(self) -> None:
        messages = _history(40, tool_result_chars=4000)
        out, _ = context_budget.compact_messages(messages, limit_tokens=1500)
        self.assertEqual(out[0]["content"], "SYSTEM PROMPT")

    def test_recent_exchanges_are_kept_verbatim(self) -> None:
        messages = _history(40, tool_result_chars=4000)
        out, _ = context_budget.compact_messages(messages, limit_tokens=3000)
        self.assertIn("answer 39", [m.get("content") for m in out])

    def test_a_summary_note_records_what_was_dropped(self) -> None:
        messages = _history(40, tool_result_chars=4000)
        out, report = context_budget.compact_messages(messages, limit_tokens=3000)
        self.assertTrue(report.dropped_units)
        note = out[1]
        self.assertEqual(note["role"], "system")
        self.assertIn("compacted", note["content"])
        self.assertIn("read_file", note["content"])

    def test_oversized_tool_results_are_trimmed_before_dropping(self) -> None:
        messages = _history(8, tool_result_chars=50_000)
        out, report = context_budget.compact_messages(
            messages, limit_tokens=40_000, max_tool_result_chars=1000
        )
        self.assertTrue(report.trimmed_results)
        trimmed = [m for m in out if m.get("role") == "tool"]
        self.assertTrue(any("trimmed from" in m["content"] for m in trimmed))

    def test_recent_huge_results_cannot_hold_history_over_budget(self) -> None:
        """A few large reads used to be un-trimmable and blew the budget."""
        messages = _history(4, tool_result_chars=60_000)
        out, _ = context_budget.compact_messages(
            messages, limit_tokens=4000, max_tool_result_chars=2000
        )
        self.assertLessEqual(context_budget.estimate_messages_tokens(out), 4000)
        self.assertTrue(_pairs_are_intact(out))

    def test_the_input_list_is_not_mutated(self) -> None:
        messages = _history(40, tool_result_chars=4000)
        before = len(messages)
        context_budget.compact_messages(messages, limit_tokens=2000)
        self.assertEqual(len(messages), before)
        self.assertNotIn("compacted", messages[1].get("content", ""))


class LiveResultCapTests(unittest.TestCase):
    """A result arrives after compaction, so it needs its own cap."""

    def test_small_results_pass_through_untouched(self) -> None:
        self.assertEqual(
            context_budget.cap_tool_result("short", window_tokens=32768), "short"
        )

    def test_a_huge_result_is_capped_with_a_note(self) -> None:
        capped = context_budget.cap_tool_result("x" * 500_000, window_tokens=8000)
        self.assertLess(len(capped), 500_000)
        self.assertIn("truncated from 500000 chars", capped)

    def test_the_cap_scales_with_the_window(self) -> None:
        self.assertLess(
            context_budget.live_tool_result_cap(8000),
            context_budget.live_tool_result_cap(128_000),
        )

    def test_no_single_result_can_fill_the_window(self) -> None:
        window = 8000
        capped = context_budget.cap_tool_result("x" * 10_000_000, window_tokens=window)
        self.assertLess(context_budget.estimate_tokens(capped), window)

    def test_the_session_applies_the_cap_when_storing_a_result(self) -> None:
        session = make_session(FakeProvider())
        from provider_base import ToolCall

        with patch.object(kara.config, "MODEL_CONTEXT_TOKENS", 8000), patch.dict(
            kara.TOOL_REGISTRY, {"read_file": lambda **kw: "y" * 400_000}
        ):
            session._execute_tool_call(
                ToolCall(id="c1", name="read_file", arguments={}), None
            )

        stored = session.messages[-1]
        self.assertEqual(stored["role"], "tool")
        self.assertLess(len(stored["content"]), 400_000)
        self.assertIn("truncated from", stored["content"])


class RepeatedCompactionTests(unittest.TestCase):
    """Regression: notes used to stack up, one permanent message per compaction."""

    def _grow_and_compact(self, rounds: int) -> list[dict]:
        messages = [{"role": "system", "content": "SYSTEM PROMPT"}]
        for r in range(rounds):
            for i in range(20):
                messages.extend(_exchange(i + r * 20, tool_result_chars=4000))
            messages, _ = context_budget.compact_messages(messages, limit_tokens=3000)
        return messages

    def test_only_one_compaction_note_ever_exists(self) -> None:
        messages = self._grow_and_compact(8)
        notes = [m for m in messages if context_budget.is_compaction_note(m)]
        self.assertEqual(len(notes), 1)

    def test_repeated_compaction_does_not_grow_the_history(self) -> None:
        four = context_budget.estimate_messages_tokens(self._grow_and_compact(4))
        twelve = context_budget.estimate_messages_tokens(self._grow_and_compact(12))
        # Three times the traffic must not meaningfully grow the retained context.
        self.assertLess(twelve, four * 1.2)

    def test_the_note_accumulates_the_running_total(self) -> None:
        first = self._grow_and_compact(1)
        later = self._grow_and_compact(4)

        def dropped(messages):
            note = next(m for m in messages if context_budget.is_compaction_note(m))
            return int("".join(c for c in note["content"].splitlines()[0] if c.isdigit()))

        self.assertGreater(dropped(later), dropped(first))

    def test_the_system_prompt_is_still_first(self) -> None:
        messages = self._grow_and_compact(6)
        self.assertEqual(messages[0]["content"], "SYSTEM PROMPT")

    def test_pairs_stay_intact_across_repeated_compaction(self) -> None:
        self.assertTrue(_pairs_are_intact(self._grow_and_compact(6)))


class SessionCompactionTests(unittest.TestCase):
    def _session(self):
        session = make_session(FakeProvider())
        session.messages = _history(40, tool_result_chars=4000)
        return session

    def test_compaction_runs_and_persists(self) -> None:
        session = self._session()
        with (
            patch.object(kara.config, "MODEL_CONTEXT_TOKENS", 8000),
            patch.object(kara.session_db, "replace_messages") as replace,
        ):
            report = session.compact_if_needed()
        self.assertIsNotNone(report)
        replace.assert_called_once()
        # Persisted history must match what the session now holds, or a restart
        # would resurrect the old messages.
        self.assertEqual(replace.call_args.args[1], session.messages)

    def test_compaction_is_skipped_when_there_is_room(self) -> None:
        session = make_session(FakeProvider())
        session.messages = _history(1)
        with (
            patch.object(kara.config, "MODEL_CONTEXT_TOKENS", 200_000),
            patch.object(kara.session_db, "replace_messages") as replace,
        ):
            self.assertIsNone(session.compact_if_needed())
        replace.assert_not_called()

    def test_a_long_session_stays_bounded_over_many_turns(self) -> None:
        """The regression this phase exists for: history used to grow forever."""
        session = make_session(FakeProvider())
        with (
            patch.object(kara.config, "MODEL_CONTEXT_TOKENS", 12_000),
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "replace_messages"),
        ):
            for i in range(60):
                session.messages.extend(_exchange(i, tool_result_chars=2000))
                session.handle_message(f"turn {i}")
                self.assertTrue(_pairs_are_intact(session.messages))

        tokens = context_budget.estimate_messages_tokens(session.messages)
        self.assertLess(tokens, 12_000)

    def test_a_batch_of_large_results_cannot_overrun_the_window(self) -> None:
        """The per-result cap bounds each result; a batch needs bounding too."""
        from provider_base import ToolCall

        names = ("web_search", "github_list_commits", "read_file")

        class _Batching:
            id = "fake"

            def __init__(self) -> None:
                self.n = 0
                self.peak = 0

            def chat(self, model, messages, tools=None):
                self.n += 1
                self.peak = max(
                    self.peak, context_budget.estimate_messages_tokens(messages)
                )
                if self.n % 2 == 1:
                    return ChatResult(
                        tool_calls=tuple(
                            ToolCall(id=f"c{self.n}_{i}", name=name, arguments={})
                            for i, name in enumerate(names)
                        ),
                        finish_reason="tool_calls",
                    )
                return ChatResult(content="done")

        provider = _Batching()
        session = make_session(provider)
        window = 16_000
        with (
            patch.object(kara.config, "MODEL_CONTEXT_TOKENS", window),
            patch.object(kara, "set_computer_request_context"),
            patch.object(kara.session_db, "clear_interrupted"),
            patch.object(kara.session_db, "record_turn"),
            patch.object(kara.session_db, "replace_messages"),
            patch.dict(kara.TOOL_REGISTRY, {n: (lambda **k: "R" * 25_000) for n in names}),
        ):
            for i in range(10):
                session.handle_message(f"turn {i}")

        self.assertLess(provider.peak, window)
        self.assertTrue(_pairs_are_intact(session.messages))

    def test_reported_prompt_tokens_are_recorded(self) -> None:
        from provider_base import Usage

        class _Reporting:
            def chat(self, model, messages, tools=None):
                return ChatResult(content="ok", usage=Usage(prompt_tokens=1234))

        session = make_session(_Reporting())
        session._chat(with_tools=False)
        self.assertEqual(session._last_prompt_tokens, 1234)
        self.assertGreaterEqual(session.context_tokens(), 1234)


if __name__ == "__main__":
    unittest.main()
