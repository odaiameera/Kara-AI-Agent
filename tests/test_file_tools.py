from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import file_tools


class FileToolsTests(unittest.TestCase):
    def test_create_read_and_search_within_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with patch.object(file_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                file_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                created = file_tools.write_file("notes/hello.txt", "alpha\nbeta", "create")
                self.assertIn("Wrote", created)
                read = file_tools.read_file("notes/hello.txt")
                self.assertIn("1: alpha", read)
                self.assertIn("2: beta", read)
                names = file_tools.search_files("hello")
                self.assertIn("hello.txt", names)
                content = file_tools.search_files("beta", search_content=True)
                self.assertIn("content:2", content)

    def test_paths_outside_allow_list_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside:
            root = Path(raw).resolve()
            target = Path(outside).resolve() / "nope.txt"
            with patch.object(file_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                file_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                self.assertIn("outside", file_tools.read_file(str(target)).lower())
                self.assertIn("outside", file_tools.write_file(str(target), "x").lower())

    def test_sensitive_files_are_blocked_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / ".env").write_text("SECRET=x", encoding="utf-8")
            with patch.object(file_tools.config, "FILE_READ_ROOTS", (root,)), patch.dict(
                "os.environ", {"KARA_ALLOW_SENSITIVE_FILES": "0"}
            ):
                self.assertIn("sensitive", file_tools.read_file(str(root / ".env")).lower())

    def test_file_info_returns_structured_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "report.txt"
            target.write_text("hello", encoding="utf-8")
            with patch.object(file_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(file_tools.file_info(str(target)))

            self.assertTrue(result["ok"])
            self.assertEqual(result["file"]["name"], "report.txt")
            self.assertEqual(result["file"]["size_bytes"], 5)
            self.assertEqual(result["file"]["kind"], "file")

    def test_copy_file_preserves_source_and_refuses_implicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "source.txt"
            destination = root / "nested" / "copy.txt"
            source.write_text("alpha", encoding="utf-8")
            with patch.object(file_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                file_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                copied = json.loads(file_tools.copy_file(str(source), str(destination)))
                refused = json.loads(file_tools.copy_file(str(source), str(destination)))

            self.assertTrue(copied["ok"])
            self.assertEqual(destination.read_text(encoding="utf-8"), "alpha")
            self.assertTrue(source.exists())
            self.assertFalse(refused["ok"])
            self.assertIn("already exists", refused["error"])

    def test_move_file_requires_source_to_be_inside_write_roots(self) -> None:
        with tempfile.TemporaryDirectory() as readable_raw, tempfile.TemporaryDirectory() as writable_raw:
            readable = Path(readable_raw).resolve()
            writable = Path(writable_raw).resolve()
            source = readable / "source.txt"
            destination = writable / "moved.txt"
            source.write_text("alpha", encoding="utf-8")
            with patch.object(file_tools.config, "FILE_READ_ROOTS", (readable, writable)), patch.object(
                file_tools.config, "FILE_WRITE_ROOTS", (writable,)
            ):
                result = json.loads(file_tools.move_file(str(source), str(destination)))

            self.assertFalse(result["ok"])
            self.assertIn("write", result["error"].lower())
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_replace_in_file_requires_an_exact_expected_match_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "module.py"
            target.write_text("name = 'old'\nname = 'old'\n", encoding="utf-8")
            with patch.object(file_tools.config, "FILE_WRITE_ROOTS", (root,)):
                refused = json.loads(
                    file_tools.replace_in_file(str(target), "name = 'old'", "name = 'new'")
                )
                changed = json.loads(
                    file_tools.replace_in_file(
                        str(target), "name = 'old'", "name = 'new'", expected_replacements=2
                    )
                )

            self.assertFalse(refused["ok"])
            self.assertIn("found 2", refused["error"])
            self.assertTrue(changed["ok"])
            self.assertEqual(changed["replacements"], 2)
            self.assertNotIn("old", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
