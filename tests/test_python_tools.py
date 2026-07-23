from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import computer_tools, python_tools


class PythonSourceToolsTests(unittest.TestCase):
    def test_inspect_python_file_returns_structure_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "sample.py"
            target.write_text(
                """\"\"\"Module docs.\"\"\"\nimport os\nfrom pathlib import Path\n\ndef greet(name: str) -> str:\n    return f'Hi {name}'\n\nclass Worker:\n    def run(self):\n        return True\n""",
                encoding="utf-8",
            )
            with patch.object(python_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(python_tools.inspect_python_file(str(target)))

            self.assertTrue(result["ok"])
            self.assertTrue(result["valid_syntax"])
            self.assertEqual(result["module_docstring"], "Module docs.")
            self.assertIn("os", result["imports"])
            self.assertEqual(result["functions"][0]["name"], "greet")
            self.assertEqual(result["classes"][0]["methods"][0]["name"], "run")

    def test_validate_python_file_returns_precise_syntax_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "broken.py"
            target.write_text("def broken(:\n    pass\n", encoding="utf-8")
            with patch.object(python_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(python_tools.validate_python_file(str(target)))

            self.assertTrue(result["ok"])
            self.assertFalse(result["valid"])
            self.assertEqual(result["error"]["line"], 1)
            self.assertIn("invalid syntax", result["error"]["message"].lower())

    def test_run_python_tests_requires_exact_later_user_approval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / "test_sample.py").write_text(
                "import unittest\n\nclass Sample(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            computer_tools._pending.clear()
            computer_tools.set_computer_request_context("python-session", "run the tests")
            with patch.object(python_tools.config, "FILE_WRITE_ROOTS", (root,)), patch.object(
                python_tools.subprocess, "run"
            ) as runner:
                requested = json.loads(python_tools.run_python_tests(str(root)))
                token = requested["approval"]["token"]
                same_turn = json.loads(
                    python_tools.run_python_tests(str(root), approval_token=token)
                )
                computer_tools.set_computer_request_context(
                    "python-session", f"approve {token}"
                )
                runner.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="test_ok ... ok\n", stderr=""
                )
                completed = json.loads(
                    python_tools.run_python_tests(str(root), approval_token=token)
                )

            self.assertEqual(requested["status"], "approval_required")
            self.assertEqual(same_turn["error"]["code"], "approval_not_confirmed")
            self.assertTrue(completed["ok"])
            self.assertTrue(completed["passed"])
            runner.assert_called_once()
            command = runner.call_args.args[0]
            self.assertEqual(command[1:4], ["-m", "unittest", "discover"])


if __name__ == "__main__":
    unittest.main()
