"""Safe, bounded SQLite inspection tools for Kara.

Connections are opened with SQLite's URI ``mode=ro``, ``query_only`` is enabled,
and an authorizer rejects mutating opcodes. SQL supplied by the model is never
interpolated into another command.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import config
from tools.file_tools import _resolve_path

_SQLITE_EXTENSIONS = {".db", ".db3", ".sqlite", ".sqlite3"}
_MAX_QUERY_CHARS = 20_000
_QUERY_TIMEOUT_SECONDS = 5.0


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"ok": False, "error": message})


def _database_path(path: str) -> Path:
    target = _resolve_path(path, config.FILE_READ_ROOTS, purpose="read")
    if not target.exists() or not target.is_file():
        raise ValueError(f"SQLite database does not exist: {target}")
    if target.suffix.casefold() not in _SQLITE_EXTENSIONS:
        raise ValueError("SQLite path must end in .db, .db3, .sqlite, or .sqlite3.")
    return target


def _connect_read_only(target: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True, timeout=2.0)
    connection.execute("PRAGMA query_only = ON")
    connection.enable_load_extension(False)
    return connection


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return {"type": "blob", "size_bytes": len(value)}
    text = str(value)
    if len(text) > 4_000:
        return text[:4_000] + f"… <{len(text) - 4_000} chars omitted>"
    return text


def inspect_sqlite_database(path: str, max_objects: int = 100) -> str:
    """Inspect tables, views, columns, and indexes in a local SQLite database.

    Args:
        path: Existing SQLite database inside an allowed read root.
        max_objects: Maximum tables/views returned (1-500).

    Returns:
        Bounded JSON schema metadata. The database is opened read-only.
    """
    connection: sqlite3.Connection | None = None
    try:
        target = _database_path(path)
        limit = max(1, min(int(max_objects), 500))
        connection = _connect_read_only(target)
        rows = connection.execute(
            """
            SELECT name, type, sql
            FROM sqlite_master
            WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            LIMIT ?
            """,
            (limit + 1,),
        ).fetchall()
        truncated = len(rows) > limit
        objects: list[dict[str, Any]] = []
        for name, object_type, create_sql in rows[:limit]:
            columns = connection.execute(
                f"PRAGMA table_info({_quoted_identifier(str(name))})"
            ).fetchall()
            indexes = connection.execute(
                f"PRAGMA index_list({_quoted_identifier(str(name))})"
            ).fetchall()
            objects.append(
                {
                    "name": name,
                    "type": object_type,
                    "columns": [
                        {
                            "position": column[0],
                            "name": column[1],
                            "data_type": column[2],
                            "not_null": bool(column[3]),
                            "default": _json_value(column[4]),
                            "primary_key_position": column[5],
                        }
                        for column in columns[:200]
                    ],
                    "indexes": [index[1] for index in indexes[:100]],
                    "create_sql": _json_value(create_sql),
                }
            )
        return _json(
            {
                "ok": True,
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "objects": objects,
                "truncated": truncated,
                "read_only": True,
            }
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(f"Could not inspect SQLite database: {exc}")
    finally:
        if connection is not None:
            connection.close()


def _read_only_authorizer(
    action: int,
    argument1: str | None,
    argument2: str | None,
    database_name: str | None,
    trigger_name: str | None,
) -> int:
    del argument2, database_name, trigger_name
    denied_names = (
        "SQLITE_ALTER_TABLE",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE",
        "SQLITE_DELETE",
        "SQLITE_DETACH",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_DROP_VTABLE",
        "SQLITE_INSERT",
        "SQLITE_PRAGMA",
        "SQLITE_REINDEX",
        "SQLITE_SAVEPOINT",
        "SQLITE_TRANSACTION",
        "SQLITE_UPDATE",
    )
    denied = {getattr(sqlite3, name) for name in denied_names if hasattr(sqlite3, name)}
    if action in denied:
        return sqlite3.SQLITE_DENY
    if action == getattr(sqlite3, "SQLITE_FUNCTION", -1) and str(argument1 or "").casefold() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def query_sqlite_database(path: str, query: str, max_rows: int = 100) -> str:
    """Run one bounded read-only SQL query against a local SQLite database.

    Args:
        path: Existing SQLite database inside an allowed read root.
        query: One SELECT, WITH, or EXPLAIN statement. Mutating opcodes are denied.
        max_rows: Maximum rows returned (1-500); one extra row detects truncation.

    Returns:
        JSON column names and rows. The query has a five-second execution budget.
    """
    connection: sqlite3.Connection | None = None
    try:
        target = _database_path(path)
        sql = query.strip()
        if not sql:
            raise ValueError("query cannot be empty.")
        if "\x00" in sql or len(sql) > _MAX_QUERY_CHARS:
            raise ValueError(f"query must be at most {_MAX_QUERY_CHARS} characters and contain no NUL bytes.")
        first_word = sql.lstrip().split(None, 1)[0].casefold().rstrip(";")
        if first_word not in {"select", "with", "explain"}:
            raise ValueError("Only SELECT, WITH, and EXPLAIN queries are allowed.")
        limit = max(1, min(int(max_rows), 500))
        connection = _connect_read_only(target)
        connection.set_authorizer(_read_only_authorizer)
        deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        cursor = connection.execute(sql)
        if cursor.description is None:
            raise ValueError("The statement did not produce a read-only result set.")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchmany(limit + 1)
        truncated = len(rows) > limit
        return _json(
            {
                "ok": True,
                "path": str(target),
                "columns": columns,
                "rows": [[_json_value(value) for value in row] for row in rows[:limit]],
                "row_count": min(len(rows), limit),
                "truncated": truncated,
                "read_only": True,
            }
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(f"Could not query SQLite database: {exc}")
    finally:
        if connection is not None:
            connection.close()
