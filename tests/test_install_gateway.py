from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install_gateway


class LinuxGatewayInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.python = self.root / ".venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("", encoding="utf-8")
        self.xdg = self.root / "xdg"
        self.xdg.mkdir()
        self.calls: list[list[str]] = []

        def fake_systemctl(args: list[str]) -> None:
            self.calls.append(args)

        self.patches = [
            patch.object(install_gateway, "PACKAGE_DIR", self.root),
            patch.object(install_gateway.sys, "platform", "linux"),
            patch.object(install_gateway, "_systemctl", fake_systemctl, create=True),
            patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.xdg)}, clear=False),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_linux_install_writes_user_unit_and_enables_it(self) -> None:
        install_gateway.install()

        unit = self.xdg / "systemd" / "user" / "kara-gateway.service"
        self.assertTrue(unit.is_file())
        text = unit.read_text(encoding="utf-8")
        self.assertIn(f'WorkingDirectory="{self.root}"', text)
        self.assertIn(f'ExecStart="{self.python}" -m gateway.run', text)
        self.assertNotIn("/home/odai", text)
        self.assertEqual(
            self.calls,
            [
                ["daemon-reload"],
                ["enable", "--now", "kara-gateway.service"],
            ],
        )

    def test_linux_uninstall_disables_and_removes_the_unit(self) -> None:
        install_gateway.install()
        self.calls.clear()
        install_gateway.uninstall()

        unit = self.xdg / "systemd" / "user" / "kara-gateway.service"
        self.assertFalse(unit.exists())
        self.assertEqual(
            self.calls,
            [
                ["disable", "--now", "kara-gateway.service"],
                ["daemon-reload"],
            ],
        )

    def test_linux_install_refuses_to_run_without_the_venv_interpreter(self) -> None:
        self.python.unlink()
        with self.assertRaises(SystemExit) as raised:
            install_gateway.install()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.calls, [])


class LinuxUnitTemplateTests(unittest.TestCase):
    def test_rendered_unit_quotes_paths_with_spaces(self) -> None:
        repo = Path("/opt/Kara Agent")
        python = repo / ".venv" / "bin" / "python"
        text = install_gateway.render_linux_unit(repo, python)
        self.assertIn('WorkingDirectory="/opt/Kara Agent"', text)
        self.assertIn('ExecStart="/opt/Kara Agent/.venv/bin/python" -m gateway.run', text)
        self.assertIn("WantedBy=default.target", text)
