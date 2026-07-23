from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import sql_tools


class SqliteToolsTests(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        path = root / "sample.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO users (name, active) VALUES ('Ada', 1), ('Grace', 0), ('Linus', 1);
            CREATE VIEW active_users AS SELECT id, name FROM users WHERE active = 1;
            """
        )
        connection.close()
        return path

    def test_inspect_sqlite_database_returns_tables_views_and_columns(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            database = self._database(root)
            with patch.object(sql_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(sql_tools.inspect_sqlite_database(str(database)))

            self.assertTrue(result["ok"])
            objects = {item["name"]: item for item in result["objects"]}
            self.assertEqual(objects["users"]["type"], "table")
            self.assertEqual(objects["active_users"]["type"], "view")
            self.assertEqual(objects["users"]["columns"][1]["name"], "name")

    def test_query_sqlite_database_is_bounded_and_structured(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            database = self._database(root)
            with patch.object(sql_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(
                    sql_tools.query_sqlite_database(
                        str(database), "SELECT id, name FROM users ORDER BY id", max_rows=2
                    )
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["columns"], ["id", "name"])
            self.assertEqual(result["rows"], [[1, "Ada"], [2, "Grace"]])
            self.assertTrue(result["truncated"])

    def test_query_sqlite_database_rejects_mutation_and_multi_statement_sql(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            database = self._database(root)
            with patch.object(sql_tools.config, "FILE_READ_ROOTS", (root,)):
                mutation = json.loads(
                    sql_tools.query_sqlite_database(str(database), "DELETE FROM users")
                )
                multiple = json.loads(
                    sql_tools.query_sqlite_database(
                        str(database), "SELECT 1; DELETE FROM users;"
                    )
                )
            connection = sqlite3.connect(database)
            count = connection.execute("SELECT count(*) FROM users").fetchone()[0]
            connection.close()

            self.assertFalse(mutation["ok"])
            self.assertFalse(multiple["ok"])
            self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
