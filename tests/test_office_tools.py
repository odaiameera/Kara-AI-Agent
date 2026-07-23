from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import office_tools


class WordDocumentTests(unittest.TestCase):
    def test_create_and_read_word_document(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "report.docx"
            with patch.object(office_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                office_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                created = json.loads(
                    office_tools.create_word_document(
                        str(target), "Quarterly Report", "First paragraph.\n\nSecond paragraph."
                    )
                )
                read_back = json.loads(office_tools.read_office_file(str(target)))

            self.assertTrue(created["ok"])
            self.assertTrue(target.exists())
            self.assertEqual(read_back["type"], "word")
            self.assertIn("Quarterly Report", read_back["paragraphs"])
            self.assertIn("Second paragraph.", read_back["paragraphs"])

    def test_append_word_text_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "notes.docx"
            with patch.object(office_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                office_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                office_tools.create_word_document(str(target), "Notes", "Original")
                appended = json.loads(office_tools.append_word_text(str(target), "Added later"))
                read_back = json.loads(office_tools.read_office_file(str(target)))

            self.assertTrue(appended["ok"])
            self.assertIn("Original", read_back["paragraphs"])
            self.assertIn("Added later", read_back["paragraphs"])


class ExcelWorkbookTests(unittest.TestCase):
    def test_create_and_read_excel_workbook_from_csv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "metrics.xlsx"
            with patch.object(office_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                office_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                created = json.loads(
                    office_tools.create_excel_workbook(
                        str(target), "Metric,Value\nUsers,42\nRevenue,99.5", "Summary"
                    )
                )
                read_back = json.loads(office_tools.read_office_file(str(target)))

            self.assertTrue(created["ok"])
            self.assertEqual(read_back["type"], "excel")
            self.assertEqual(read_back["sheets"][0]["name"], "Summary")
            self.assertEqual(read_back["sheets"][0]["rows"][1], ["Users", 42])

    def test_set_excel_cell_updates_an_existing_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "metrics.xlsx"
            with patch.object(office_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                office_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                office_tools.create_excel_workbook(str(target), "Metric,Value\nUsers,42", "Summary")
                updated = json.loads(
                    office_tools.set_excel_cell(str(target), "B2", "100", "Summary")
                )
                read_back = json.loads(office_tools.read_office_file(str(target)))

            self.assertTrue(updated["ok"])
            self.assertEqual(read_back["sheets"][0]["rows"][1], ["Users", 100])


class PowerPointTests(unittest.TestCase):
    def test_create_and_read_powerpoint_from_json_slides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "briefing.pptx"
            slides = json.dumps(
                [
                    {"title": "Status", "bullets": ["All systems operational", "Tests passing"]},
                    {"title": "Next", "bullets": ["Add more tools"]},
                ]
            )
            with patch.object(office_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                office_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                created = json.loads(
                    office_tools.create_powerpoint(str(target), "Kara Briefing", slides)
                )
                read_back = json.loads(office_tools.read_office_file(str(target)))

            self.assertTrue(created["ok"])
            self.assertEqual(read_back["type"], "powerpoint")
            self.assertEqual(len(read_back["slides"]), 3)
            self.assertIn("Kara Briefing", read_back["slides"][0]["text"])
            self.assertIn("Tests passing", read_back["slides"][1]["text"])

    def test_append_powerpoint_slide_preserves_existing_slides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "briefing.pptx"
            with patch.object(office_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                office_tools.config, "FILE_WRITE_ROOTS", (root,)
            ):
                office_tools.create_powerpoint(
                    str(target), "Briefing", '[{"title":"First","bullets":["One"]}]'
                )
                appended = json.loads(
                    office_tools.append_powerpoint_slide(
                        str(target), "Second", '["Two", "Three"]'
                    )
                )
                read_back = json.loads(office_tools.read_office_file(str(target)))

            self.assertTrue(appended["ok"])
            self.assertEqual(len(read_back["slides"]), 3)
            self.assertIn("Three", read_back["slides"][2]["text"])


if __name__ == "__main__":
    unittest.main()
