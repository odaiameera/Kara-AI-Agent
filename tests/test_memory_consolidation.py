from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config


class _BrainSandbox(unittest.TestCase):
    """Point the whole brain at a temp dir so tests never touch real memory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            patch.object(config, "BRAIN_DIR", root),
            patch.object(config, "CORE_DIR", root / "core"),
            patch.object(config, "LEARNINGS_DIR", root / "learnings"),
            patch.object(config, "SESSIONS_DIR", root / "sessions"),
            patch.object(config, "INDEX_DIR", root / "index"),
            patch.object(config, "INDEX_FILE", root / "index" / "index.json"),
            patch.object(config, "LOG_DIR", root / "logs"),
            patch.object(
                config, "SESSIONS_MIGRATED_MARKER", root / "sessions_migrated.marker"
            ),
            patch.object(
                config,
                "CORE_FILES",
                {
                    "persona": root / "core" / "persona.md",
                    "human": root / "core" / "human.md",
                    "active_task": root / "core" / "active_task.md",
                },
            ),
        ]
        for p in self._patches:
            p.start()
        for d in (root, root / "core", root / "learnings", root / "sessions", root / "index"):
            d.mkdir(parents=True, exist_ok=True)

        import session_db

        self.session_db = session_db
        self._db_patch = patch.object(session_db, "DB_PATH", root / "state.db")
        self._db_patch.start()
        session_db._initialized = False
        session_db.init_db()

        import memory_store
        import vector_index

        self.memory_store = importlib.reload(memory_store)
        self.vector_index = importlib.reload(vector_index)
        self.root = root

    def tearDown(self) -> None:
        self._db_patch.stop()
        for p in reversed(self._patches):
            p.stop()
        self.session_db._initialized = False
        self._tmp.cleanup()


class SessionSummaryStoreTests(_BrainSandbox):
    def test_summary_round_trips(self) -> None:
        self.session_db.save_session_summary("kara:cli:local", "Recap", "We fixed the gateway.")
        rows = self.session_db.load_session_summaries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "We fixed the gateway.")
        self.assertEqual(rows[0]["session_key"], "kara:cli:local")

    def test_blank_summary_is_not_stored(self) -> None:
        self.assertEqual(self.session_db.save_session_summary("k", "t", "   "), 0)
        self.assertEqual(self.session_db.load_session_summaries(), [])

    def test_fingerprint_changes_when_a_summary_is_added(self) -> None:
        before = self.session_db.session_summary_fingerprint()
        self.session_db.save_session_summary("k", "t", "something new")
        self.assertNotEqual(before, self.session_db.session_summary_fingerprint())


class TranscriptWritersAreGoneTests(_BrainSandbox):
    def test_markdown_transcript_helpers_no_longer_exist(self) -> None:
        for name in ("start_session", "log_turn", "finalize_session"):
            self.assertFalse(
                hasattr(self.memory_store, name),
                f"memory_store.{name} should have been removed",
            )

    def test_learnings_are_still_markdown(self) -> None:
        path = self.memory_store.save_learning("Odai prefers uv", "Use uv, not pip.")
        self.assertTrue(path.exists())
        self.assertIn("Odai prefers uv", path.read_text(encoding="utf-8"))


class EndSessionTests(_BrainSandbox):
    def _session(self, summary: str):
        import kara

        session = kara.KaraSession.__new__(kara.KaraSession)
        session.session_key = "kara:cli:local"
        session.channel = "cli"
        session.model = "test-model"
        session.messages = []
        session.allowed_tool_names = None
        session.active_groups = set()
        session._persist = Mock()
        session.provider = Mock()
        session.provider.chat.return_value = {
            "message": {"role": "assistant", "content": summary}
        }
        return kara, session

    def test_summary_is_stored_exactly_once(self) -> None:
        kara, session = self._session("We consolidated Kara's memory.")
        with (
            patch.object(kara.session_db, "mark_interrupted"),
            patch.object(kara.session_db, "save_session_summary") as saved,
        ):
            session.end_session()
        saved.assert_called_once()
        self.assertEqual(saved.call_args.args[2], "We consolidated Kara's memory.")

    def test_summary_is_not_also_written_as_a_learning(self) -> None:
        kara, session = self._session("A recap worth keeping.")
        with patch.object(kara.session_db, "mark_interrupted"):
            session.end_session()

        learnings = list(self.root.glob("learnings/*.md"))
        self.assertEqual(learnings, [], "summary should not be duplicated as a learning")
        self.assertEqual(len(self.session_db.load_session_summaries()), 1)

    def test_no_session_markdown_is_created(self) -> None:
        kara, session = self._session("Recap.")
        with patch.object(kara.session_db, "mark_interrupted"):
            session.end_session()
        self.assertEqual(list(self.root.glob("sessions/*.md")), [])


class VectorIndexSourceTests(_BrainSandbox):
    def test_sources_cover_learnings_and_summaries(self) -> None:
        self.memory_store.save_learning("Deploy notes", "Kara runs on Windows.")
        self.session_db.save_session_summary("kara:cli:local", "Recap", "We shipped gating.")

        records = self.vector_index._sources()
        keys = [r.key for r in records]
        self.assertTrue(any(k.startswith("learnings/") for k in keys))
        self.assertTrue(any(k.startswith("summary:") for k in keys))
        self.assertIn("We shipped gating.", [r.text for r in records])

    def test_raw_session_markdown_is_not_indexed(self) -> None:
        (self.root / "sessions").mkdir(parents=True, exist_ok=True)
        (self.root / "sessions" / "2026-01-01-120000.md").write_text(
            "# Session\n\n**You:** hey\n\n**Kara:** hi\n", encoding="utf-8"
        )
        keys = [r.key for r in self.vector_index._sources()]
        self.assertEqual([k for k in keys if "sessions/" in k], [])

    def test_keyword_search_finds_a_stored_summary(self) -> None:
        self.session_db.save_session_summary(
            "kara:cli:local", "Recap", "We replaced the restart mechanism."
        )
        results = self.vector_index._keyword_only_search("restart mechanism", 5)
        self.assertTrue(results)
        self.assertIn("restart", results[0]["text"])

    def test_a_summary_appears_only_once_in_search(self) -> None:
        text = "We consolidated the memory layers."
        self.session_db.save_session_summary("kara:cli:local", "Recap", text)
        results = self.vector_index._keyword_only_search("consolidated memory layers", 10)
        self.assertEqual(sum(1 for r in results if text in r["text"]), 1)

    def test_fingerprint_reacts_to_new_summaries(self) -> None:
        before = self.vector_index._memory_fingerprint()
        self.session_db.save_session_summary("k", "t", "brand new recap")
        self.assertNotEqual(before, self.vector_index._memory_fingerprint())


class LegacyMigrationTests(_BrainSandbox):
    def _write_log(self, name: str, body: str) -> None:
        (self.root / "sessions" / name).write_text(body, encoding="utf-8")

    def test_summaries_are_lifted_out_of_old_logs(self) -> None:
        self._write_log(
            "2026-01-01-101010.md",
            "# Session 2026-01-01-101010\n\n**You:** hi\n\n"
            "---\n\n## Summary\n\nWe planned the refactor.\n",
        )
        imported = self.memory_store.migrate_legacy_session_logs()
        self.assertEqual(imported, 1)
        rows = self.session_db.load_session_summaries()
        self.assertEqual(rows[0]["summary"], "We planned the refactor.")

    def test_logs_without_a_summary_are_skipped(self) -> None:
        self._write_log("2026-01-02-101010.md", "# Session\n\n**You:** just chatter\n")
        self.assertEqual(self.memory_store.migrate_legacy_session_logs(), 0)
        self.assertEqual(self.session_db.load_session_summaries(), [])

    def test_migration_does_not_delete_the_original_files(self) -> None:
        self._write_log(
            "2026-01-03-101010.md", "# Session\n\n## Summary\n\nKept safe.\n"
        )
        self.memory_store.migrate_legacy_session_logs()
        self.assertTrue((self.root / "sessions" / "2026-01-03-101010.md").exists())

    def test_migration_runs_only_once(self) -> None:
        self._write_log("2026-01-04-101010.md", "# Session\n\n## Summary\n\nOnce only.\n")
        self.assertEqual(self.memory_store.migrate_legacy_session_logs(), 1)
        self.assertEqual(self.memory_store.migrate_legacy_session_logs(), 0)
        self.assertEqual(len(self.session_db.load_session_summaries()), 1)


if __name__ == "__main__":
    unittest.main()
