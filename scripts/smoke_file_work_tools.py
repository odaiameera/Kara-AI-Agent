"""Create and verify real artifacts through Kara's file-work tool surface."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from tools.file_tools import copy_file, move_file, replace_in_file, write_file
from tools.office_tools import (
    append_powerpoint_slide,
    append_word_text,
    create_excel_workbook,
    create_powerpoint,
    create_word_document,
    read_office_file,
    set_excel_cell,
)
from tools.python_tools import inspect_python_file, validate_python_file
from tools.sql_tools import inspect_sqlite_database, query_sqlite_database

OUTPUT = PROJECT / "brain" / "toolkit_smoke"


def checked(raw: str, label: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not payload.get("ok"):
        raise RuntimeError(f"{label} failed: {payload}")
    return payload


def main() -> None:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    source = OUTPUT / "notes.txt"
    copy = OUTPUT / "notes-copy.txt"
    moved = OUTPUT / "notes-final.txt"
    write_file(str(source), "alpha\nbeta\n", mode="create")
    checked(replace_in_file(str(source), "beta", "gamma"), "replace text")
    checked(copy_file(str(source), str(copy)), "copy text")
    checked(move_file(str(copy), str(moved)), "move text")

    word = OUTPUT / "brief.docx"
    checked(create_word_document(str(word), "Kara Toolkit", "Word document created."), "create Word")
    checked(append_word_text(str(word), "Second paragraph appended."), "append Word")
    word_data = checked(read_office_file(str(word)), "read Word")

    excel = OUTPUT / "metrics.xlsx"
    checked(
        create_excel_workbook(str(excel), "Metric,Value\nTests,66\nTools,43", "Status"),
        "create Excel",
    )
    checked(set_excel_cell(str(excel), "B2", "67", "Status"), "update Excel")
    excel_data = checked(read_office_file(str(excel)), "read Excel")

    powerpoint = OUTPUT / "briefing.pptx"
    checked(
        create_powerpoint(
            str(powerpoint),
            "Kara File Toolkit",
            '[{"title":"Status","bullets":["Office files operational","SQLite is read-only"]}]',
        ),
        "create PowerPoint",
    )
    checked(
        append_powerpoint_slide(str(powerpoint), "Python", '["AST inspection", "Approved unittest execution"]'),
        "append PowerPoint",
    )
    powerpoint_data = checked(read_office_file(str(powerpoint)), "read PowerPoint")

    database = OUTPUT / "sample.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "INSERT INTO items (name) VALUES ('Word'), ('Excel'), ('PowerPoint');"
    )
    connection.close()
    schema_data = checked(inspect_sqlite_database(str(database)), "inspect SQLite")
    query_data = checked(
        query_sqlite_database(str(database), "SELECT id, name FROM items ORDER BY id", 10),
        "query SQLite",
    )

    module = OUTPUT / "sample.py"
    module.write_text(
        '"""Smoke module."""\n\ndef answer() -> int:\n    return 42\n',
        encoding="utf-8",
    )
    test_module = OUTPUT / "test_sample.py"
    test_module.write_text(
        "import unittest\nfrom sample import answer\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_answer(self):\n"
        "        self.assertEqual(answer(), 42)\n",
        encoding="utf-8",
    )
    python_data = checked(inspect_python_file(str(module)), "inspect Python")
    validation_data = checked(validate_python_file(str(module)), "validate Python")
    if not validation_data.get("valid"):
        raise RuntimeError(f"Python validation failed: {validation_data}")

    summary = {
        "ok": True,
        "output": str(OUTPUT),
        "artifacts": sorted(path.name for path in OUTPUT.iterdir()),
        "word_paragraphs": len(word_data["paragraphs"]),
        "excel_value_b2": excel_data["sheets"][0]["rows"][1][1],
        "powerpoint_slides": powerpoint_data["slide_count"],
        "sqlite_objects": len(schema_data["objects"]),
        "sqlite_rows": query_data["row_count"],
        "python_functions": [item["name"] for item in python_data["functions"]],
        "python_valid": validation_data["valid"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
