import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import install_buzz_acp_linux


class BuzzACPInstallerTests(unittest.TestCase):
    @mock.patch("scripts.install_buzz_acp_linux.shutil.which")
    def test_rendered_service_uses_python_script_shape_and_controlled_path(self, which):
        which.side_effect = lambda name: {
            "python": "/stable/kara/.venv/bin/python",
            "buzz": "/home/odai/.local/bin/buzz",
            "buzz-acp": "/usr/bin/buzz-acp",
        }.get(name)
        unit, env_example = install_buzz_acp_linux.rendered_unit()
        self.assertIn("ExecStart=/usr/bin/buzz-acp", unit)
        self.assertIn("ExecStartPre=", unit)
        self.assertIn("buzz_identity.py check", unit)
        self.assertIn("Environment=PATH=/home/odai/.local/bin:/usr/bin", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("ProtectSystem=full", unit)
        expected_python = install_buzz_acp_linux.PACKAGE_DIR / ".venv" / "bin" / "python"
        self.assertIn(f"BUZZ_ACP_AGENT_COMMAND={expected_python}", env_example)
        self.assertIn("BUZZ_ACP_AGENT_ARGS=", env_example)
        self.assertIn("acp_server.py", env_example)
        self.assertNotIn("run python acp_server.py", env_example)

    def test_validate_auth_tag_accepts_compact_json_array(self):
        install_buzz_acp_linux.validate_auth_tag(
            json.dumps(["auth", "owner", "", "signature"], separators=(",", ":"))
        )

    def test_validate_auth_tag_rejects_unparseable_value(self):
        with self.assertRaisesRegex(ValueError, "BUZZ_AUTH_TAG"):
            install_buzz_acp_linux.validate_auth_tag("[auth,owner,,signature]")

    def test_disposable_checkout_is_rejected_for_install(self):
        checkout = Path("/home/odai/.buzz/.scratch/kara-linux-buzz-acp")
        with self.assertRaisesRegex(SystemExit, "stable checkout"):
            install_buzz_acp_linux.ensure_stable_checkout(checkout)

    def test_stable_checkout_is_accepted_for_install(self):
        with tempfile.TemporaryDirectory(prefix="kara-stable-") as directory:
            install_buzz_acp_linux.ensure_stable_checkout(Path(directory))


if __name__ == "__main__":
    unittest.main()
