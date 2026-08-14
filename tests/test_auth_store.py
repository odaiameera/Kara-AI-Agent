from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auth import store as auth_store
from auth import codex as codex_auth
import config
from auth import github as github_auth


class SharedAuthStoreTests(unittest.TestCase):
    """codex_auth and github_auth both persist into one brain/auth.json, so the
    store layer they share lives in auth_store.py. These pin the properties that
    duplication used to put at risk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        patcher = patch.object(config, "BRAIN_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_provider_write_leaves_the_other_intact(self) -> None:
        codex_auth.save_tokens({"access_token": "c-a", "refresh_token": "c-r"})
        github_auth.save_tokens({"access_token": "g-a", "scope": "repo"})

        data = json.loads((self.root / "auth.json").read_text(encoding="utf-8"))

        self.assertEqual(sorted(data["providers"]), ["github", "openai-codex"])
        self.assertEqual(codex_auth.read_tokens()["tokens"]["access_token"], "c-a")
        self.assertEqual(github_auth.read_tokens()["tokens"]["access_token"], "g-a")

    def test_provider_errors_share_a_base_so_a_corrupt_store_is_caught(self) -> None:
        # Both provider error types subclass AuthStoreError; load_store raises the
        # base, so `except <Provider>AuthError` alone would let a corrupt file escape.
        self.assertTrue(issubclass(codex_auth.CodexAuthError, auth_store.AuthStoreError))
        self.assertTrue(issubclass(github_auth.GitHubAuthError, auth_store.AuthStoreError))

        codex_auth.save_tokens({"access_token": "c-a", "refresh_token": "c-r"})
        (self.root / "auth.json").write_text("{not json", encoding="utf-8")

        self.assertFalse(codex_auth.has_credentials())
        self.assertFalse(github_auth.has_credentials())

    def test_read_provider_returns_none_when_absent(self) -> None:
        self.assertIsNone(auth_store.read_provider("github"))

    def test_write_provider_creates_the_brain_directory(self) -> None:
        nested = self.root / "does-not-exist-yet"
        with patch.object(config, "BRAIN_DIR", nested):
            auth_store.write_provider("github", {"tokens": {"access_token": "x"}})
            self.assertTrue((nested / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
