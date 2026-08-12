from __future__ import annotations

import json
import unittest

import kara
import scheduled_runner
from tools import registry


class RegistryAggregationTests(unittest.TestCase):
    def test_every_declared_tool_is_registered_once(self) -> None:
        names = [fn.__name__ for fn in registry.ALL_TOOLS]
        self.assertEqual(len(names), len(set(names)), "duplicate tool names")
        self.assertEqual(set(names), set(registry.TOOL_REGISTRY))
        self.assertEqual(
            set(names), {item["function"]["name"] for item in registry.TOOL_SCHEMAS}
        )

    def test_kara_reexports_the_registry(self) -> None:
        self.assertIs(kara.TOOL_REGISTRY, registry.TOOL_REGISTRY)
        self.assertIs(kara.TOOL_SCHEMAS, registry.TOOL_SCHEMAS)

    def test_groups_partition_the_tool_set(self) -> None:
        grouped: list[str] = []
        for names in registry.GROUPS.values():
            grouped.extend(names)
        self.assertEqual(sorted(grouped), sorted(registry.TOOL_REGISTRY))

    def test_every_tool_resolves_to_exactly_one_group(self) -> None:
        for name in registry.TOOL_REGISTRY:
            group = registry.group_of(name)
            self.assertIsNotNone(group, f"{name} has no group")
            self.assertIn(name, registry.GROUPS[group])


class ScheduledSafeBoundaryTests(unittest.TestCase):
    """The scheduled-run allowlist is a security boundary; pin it exactly."""

    EXPECTED = {
        "search_memory",
        "web_search",
        "web_fetch",
        "list_directory",
        "read_file",
        "search_files",
        "file_info",
        "read_office_file",
        # PDF and OCR extraction: read-only like read_office_file, but omitted
        # when this list was written because document_tools.py did not exist yet.
        "read_pdf",
        "ocr_image",
        "inspect_sqlite_database",
        "query_sqlite_database",
        "inspect_python_file",
        "validate_python_file",
        "system_overview",
        "list_processes",
        "list_services",
        "list_scheduled_tasks",
        "disk_usage",
        "search_obsidian",
        "read_obsidian_note",
    }

    def test_derived_allowlist_matches_the_declared_boundary(self) -> None:
        self.assertEqual(set(registry.SCHEDULED_SAFE), self.EXPECTED)
        self.assertEqual(set(scheduled_runner.SAFE_AGENT_TOOLS), self.EXPECTED)

    def test_allowlist_resolves_fully_in_the_registry(self) -> None:
        self.assertTrue(set(registry.SCHEDULED_SAFE).issubset(registry.TOOL_REGISTRY))

    def test_no_write_or_send_tool_is_scheduled_safe(self) -> None:
        forbidden = {
            "write_file",
            "copy_file",
            "move_file",
            "replace_in_file",
            "email_send",
            "computer_use",
            "run_python_tests",
            "git_push_changes",
            "github_create_issue",
            "github_create_pull_request",
            "github_merge_pull_request",
            "schedule_agent_job",
            "core_memory_append",
            "save_learning",
            "write_obsidian_note",
        }
        self.assertEqual(forbidden & set(registry.SCHEDULED_SAFE), set())


class GatingTests(unittest.TestCase):
    def test_always_on_groups_exclude_the_heavy_integrations(self) -> None:
        for group in ("github", "email", "office", "computer", "windows", "scheduler"):
            self.assertIn(group, registry.ON_DEMAND_GROUPS)

    def test_baseline_surface_is_far_smaller_than_the_full_one(self) -> None:
        full = len(json.dumps(registry.TOOL_SCHEMAS))
        base = len(json.dumps(registry.schemas_for_groups(set(registry.ALWAYS_ON))))
        self.assertLess(base, full / 3)

    def test_activate_tool_group_is_offered_while_groups_are_hidden(self) -> None:
        base = registry.schemas_for_groups(set(registry.ALWAYS_ON))
        self.assertIn(
            registry.ACTIVATE_TOOL, {item["function"]["name"] for item in base}
        )

    def test_activate_tool_group_disappears_once_everything_is_loaded(self) -> None:
        every = registry.schemas_for_groups(set(registry.GROUPS))
        names = {item["function"]["name"] for item in every}
        self.assertNotIn(registry.ACTIVATE_TOOL, names)
        self.assertEqual(names, set(registry.TOOL_REGISTRY))

    def test_keywords_activate_the_expected_groups(self) -> None:
        cases = [
            ("open a PR on my kara repo", "github"),
            ("check my inbox for unread mail", "email"),
            ("build me an excel workbook", "office"),
            ("remind me every morning to stretch", "scheduler"),
            ("what does mnemosyne have on this", "mnemosyne"),
            ("search my obsidian vault", "obsidian"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertIn(expected, registry.groups_for_text(text))

    def test_plain_chat_activates_nothing(self) -> None:
        self.assertEqual(registry.groups_for_text("how are you today?"), set())

    def test_keywords_match_on_word_boundaries(self) -> None:
        # "pr" is a github keyword; it must not fire inside ordinary words.
        self.assertNotIn("github", registry.groups_for_text("I prefer the press"))


if __name__ == "__main__":
    unittest.main()
