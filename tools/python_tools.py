"""Python source inspection, validation, and approval-gated test execution."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any

import config
from tools.file_tools import _resolve_path

_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_SYMBOLS = 500


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"ok": False, "error": message})


def _python_path(path: str) -> Path:
    target = _resolve_path(path, config.FILE_READ_ROOTS, purpose="read")
    if not target.exists() or not target.is_file():
        raise ValueError(f"Python file does not exist: {target}")
    if target.suffix.casefold() not in {".py", ".pyw"}:
        raise ValueError("Python source path must end in .py or .pyw.")
    if target.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError(f"Python source is limited to {_MAX_SOURCE_BYTES} bytes.")
    return target


def _read_python_source(target: Path) -> str:
    with tokenize.open(target) as handle:
        return handle.read()


def _syntax_error(exc: SyntaxError) -> dict[str, Any]:
    return {
        "message": exc.msg,
        "line": exc.lineno,
        "column": exc.offset,
        "end_line": exc.end_lineno,
        "end_column": exc.end_offset,
        "text": (exc.text or "").rstrip("\r\n")[:500],
    }


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    names = [argument.arg for argument in [*node.args.posonlyargs, *node.args.args]]
    if node.args.vararg:
        names.append("*" + node.args.vararg.arg)
    names.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg:
        names.append("**" + node.args.kwarg.arg)
    return names


def _function_record(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    return {
        "name": node.name,
        "line": node.lineno,
        "end_line": node.end_lineno,
        "async": isinstance(node, ast.AsyncFunctionDef),
        "parameters": _parameters(node),
        "docstring": (ast.get_docstring(node, clean=True) or "")[:1_000],
    }


def inspect_python_file(path: str, max_symbols: int = 200) -> str:
    """Inspect imports, functions, classes, methods, and syntax without executing code.

    Args:
        path: Existing .py or .pyw file inside an allowed read root.
        max_symbols: Maximum imports/functions/classes returned (1-500).

    Returns:
        Structured JSON source metadata and syntax diagnostics.
    """
    try:
        target = _python_path(path)
        source = _read_python_source(target)
        limit = max(1, min(int(max_symbols), _MAX_SYMBOLS))
        try:
            tree = ast.parse(source, filename=str(target), type_comments=True)
        except SyntaxError as exc:
            return _json(
                {
                    "ok": True,
                    "path": str(target),
                    "valid_syntax": False,
                    "line_count": len(source.splitlines()),
                    "error": _syntax_error(exc),
                }
            )
        imports: list[str] = []
        functions: list[dict[str, Any]] = []
        classes: list[dict[str, Any]] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                imports.append(module)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(_function_record(node))
            elif isinstance(node, ast.ClassDef):
                methods = [
                    _function_record(item)
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                classes.append(
                    {
                        "name": node.name,
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                        "bases": [ast.unparse(base)[:300] for base in node.bases],
                        "docstring": (ast.get_docstring(node, clean=True) or "")[:1_000],
                        "methods": methods[:limit],
                    }
                )
        total_symbols = len(imports) + len(functions) + len(classes)
        return _json(
            {
                "ok": True,
                "path": str(target),
                "valid_syntax": True,
                "line_count": len(source.splitlines()),
                "size_bytes": target.stat().st_size,
                "module_docstring": (ast.get_docstring(tree, clean=True) or "")[:1_000],
                "imports": sorted(dict.fromkeys(imports))[:limit],
                "functions": functions[:limit],
                "classes": classes[:limit],
                "truncated": total_symbols > limit,
            }
        )
    except (UnicodeError, ValueError, PermissionError, OSError) as exc:
        return _error(f"Could not inspect Python file: {exc}")


def validate_python_file(path: str) -> str:
    """Compile a Python source file for syntax validation without executing it.

    Args:
        path: Existing .py or .pyw file inside an allowed read root.

    Returns:
        JSON validity result with exact syntax-error location when invalid.
    """
    try:
        target = _python_path(path)
        source = _read_python_source(target)
        try:
            compile(source, str(target), "exec", dont_inherit=True, optimize=0)
        except SyntaxError as exc:
            return _json(
                {
                    "ok": True,
                    "path": str(target),
                    "valid": False,
                    "line_count": len(source.splitlines()),
                    "error": _syntax_error(exc),
                    "executed": False,
                }
            )
        return _json(
            {
                "ok": True,
                "path": str(target),
                "valid": True,
                "line_count": len(source.splitlines()),
                "size_bytes": target.stat().st_size,
                "executed": False,
            }
        )
    except (UnicodeError, ValueError, PermissionError, OSError) as exc:
        return _error(f"Could not validate Python file: {exc}")


def _test_child_environment(root: Path) -> dict[str, str]:
    blocked = ("api_key", "apikey", "token", "password", "secret", "credential", "cookie")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(fragment in key.casefold() for fragment in blocked)
    }
    environment.pop("PYTHONHOME", None)
    environment["PYTHONPATH"] = str(root)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def run_python_tests(
    root: str = ".",
    pattern: str = "test*.py",
    timeout_seconds: int = 60,
    approval_token: str = "",
) -> str:
    """Run fixed ``unittest discover`` after Kara's exact two-turn approval.

    Args:
        root: Test project directory inside an allowed write root.
        pattern: Filename glob such as test*.py; path separators are rejected.
        timeout_seconds: Execution timeout clamped to 5-300 seconds.
        approval_token: Token supplied only after the user replies exactly ``approve TOKEN``.

    Returns:
        Approval request or bounded unittest output. No shell or arbitrary command is accepted.
    """
    try:
        from tools import computer_tools

        target = _resolve_path(root, config.FILE_WRITE_ROOTS, purpose="write")
        if not target.exists() or not target.is_dir():
            raise ValueError(f"Python test root does not exist or is not a directory: {target}")
        selected_pattern = pattern.strip()
        if (
            not selected_pattern
            or len(selected_pattern) > 100
            or "/" in selected_pattern
            or "\\" in selected_pattern
            or not re.fullmatch(r"[A-Za-z0-9_.?*\-]+", selected_pattern)
            or not selected_pattern.casefold().endswith(".py")
        ):
            raise ValueError("pattern must be a simple .py filename glob without path separators.")
        timeout = max(5, min(int(timeout_seconds), 300))
        intent = {
            "root": str(target),
            "pattern": selected_pattern,
            "timeout_seconds": timeout,
        }
        approval_target = {
            "pid": 0,
            "window_id": 0,
            "title": str(target),
            "app_name": "Python unittest",
        }
        approval = computer_tools._approval_gate(
            "run_python_tests",
            intent,
            approval_target,
            approval_token,
            f"run Python unittest discovery inside '{target}'",
        )
        if approval:
            return approval
        command = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(target),
            "-p",
            selected_pattern,
            "-v",
        ]
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                cwd=target,
                env=_test_child_environment(target),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            partial = "\n".join(
                part for part in (str(exc.stdout or ""), str(exc.stderr or "")) if part
            )
            return _json(
                {
                    "ok": False,
                    "status": "timeout",
                    "passed": False,
                    "timeout_seconds": timeout,
                    "output": partial[-20_000:],
                }
            )
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip()
        )
        if len(output) > 20_000:
            output = output[-20_000:] + "\n... output truncated to final 20000 characters"
        return _json(
            {
                "ok": True,
                "status": "completed",
                "passed": completed.returncode == 0,
                "exit_code": completed.returncode,
                "root": str(target),
                "pattern": selected_pattern,
                "output": output,
            }
        )
    except (ValueError, PermissionError, OSError) as exc:
        return _error(f"Could not run Python tests: {exc}")

# --- Registry declaration ------------------------------------------------------
# Consumed by tools.registry; this is the single source of truth for which
# functions in this module are exposed to the model and which of them are safe
# for unattended scheduled runs.
TOOL_GROUP = "python"

TOOLS = [
    inspect_python_file,
    validate_python_file,
    run_python_tests,
]

SCHEDULED_SAFE = {
    "inspect_python_file",
    "validate_python_file",
}
