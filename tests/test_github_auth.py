from __future__ import annotations

import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import github_auth


class TokenStoreTests(unittest.TestCase):
    def test_save_and_read_tokens_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with patch.object(config, "BRAIN_DIR", root):
                github_auth.save_tokens({"access_token": "tok1", "scope": "repo workflow"})
                state = github_auth.read_tokens()

        self.assertEqual(state["tokens"]["access_token"], "tok1")
        self.assertEqual(state["scope"], "repo workflow")
        self.assertNotIn("refresh_token", state["tokens"])

    def test_save_tokens_without_access_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with patch.object(config, "BRAIN_DIR", root):
                with self.assertRaises(github_auth.GitHubAuthError):
                    github_auth.save_tokens({"scope": "repo"})

    def test_read_tokens_missing_raises_clear_login_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with patch.object(config, "BRAIN_DIR", root):
                with self.assertRaises(github_auth.GitHubAuthError) as ctx:
                    github_auth.read_tokens()
        self.assertIn("login", str(ctx.exception).lower())

    def test_has_credentials_reflects_store_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with patch.object(config, "BRAIN_DIR", root):
                self.assertFalse(github_auth.has_credentials())
                github_auth.save_tokens({"access_token": "tok1"})
                self.assertTrue(github_auth.has_credentials())

    def test_refresh_tokens_without_refresh_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with patch.object(config, "BRAIN_DIR", root):
                github_auth.save_tokens({"access_token": "tok1"})
                with self.assertRaises(github_auth.GitHubAuthError):
                    github_auth.refresh_tokens()


class GitCredentialHelperTests(unittest.TestCase):
    def test_get_returns_token_only_for_https_github(self) -> None:
        output = io.StringIO()
        with patch.object(
            github_auth,
            "runtime_credentials",
            return_value={"access_token": "tok1", "scope": "repo"},
        ) as credentials:
            code = github_auth.git_credential_helper(
                "get",
                input_stream=io.StringIO("protocol=https\nhost=github.com\n\n"),
                output_stream=output,
            )

        self.assertEqual(code, 0)
        self.assertEqual(
            output.getvalue(),
            "username=x-access-token\npassword=tok1\n",
        )
        credentials.assert_called_once_with(refresh_if_expiring=False)

    def test_get_refuses_foreign_hosts_without_reading_token_store(self) -> None:
        output = io.StringIO()
        with patch.object(github_auth, "runtime_credentials") as credentials:
            code = github_auth.git_credential_helper(
                "get",
                input_stream=io.StringIO("protocol=https\nhost=example.com\n\n"),
                output_stream=output,
            )

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "")
        credentials.assert_not_called()

    def test_get_reports_missing_credentials_without_traceback(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(
            github_auth,
            "runtime_credentials",
            side_effect=github_auth.GitHubAuthError("login required"),
        ):
            code = github_auth.git_credential_helper(
                "get",
                input_stream=io.StringIO("protocol=https\nhost=github.com\n\n"),
                output_stream=output,
                error_stream=errors,
            )

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("credentials unavailable", errors.getvalue().lower())
        self.assertNotIn("traceback", errors.getvalue().lower())


class ExpiryTests(unittest.TestCase):
    def test_access_token_is_expiring_true_for_past_expiry(self) -> None:
        state = {"access_token_expires_at": time.time() - 10}
        self.assertTrue(github_auth.access_token_is_expiring(state))

    def test_access_token_is_expiring_false_for_future_expiry(self) -> None:
        state = {"access_token_expires_at": time.time() + 10_000}
        self.assertFalse(github_auth.access_token_is_expiring(state))

    def test_access_token_is_expiring_false_when_no_expiry_recorded(self) -> None:
        # Classic (non-expiring) GitHub OAuth tokens never record an expiry.
        self.assertFalse(github_auth.access_token_is_expiring({}))


class DeviceLoginTests(unittest.TestCase):
    def test_device_login_polls_until_authorized(self) -> None:
        device_resp = MagicMock(status_code=200)
        device_resp.json.return_value = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 1,
            "expires_in": 900,
        }
        pending_resp = MagicMock(status_code=200)
        pending_resp.json.return_value = {"error": "authorization_pending"}
        success_resp = MagicMock(status_code=200)
        success_resp.json.return_value = {
            "access_token": "tok-abc",
            "token_type": "bearer",
            "scope": "repo",
        }

        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = [device_resp, pending_resp, success_resp]

        with patch.object(github_auth.httpx, "Client", return_value=client), patch.object(
            github_auth.time, "sleep", return_value=None
        ), patch.dict("os.environ", {"GITHUB_CLIENT_ID": "client-xyz"}):
            payload = github_auth.device_login(print_fn=lambda *_args: None)

        self.assertEqual(payload["access_token"], "tok-abc")
        self.assertEqual(client.post.call_count, 3)

    def test_device_login_raises_on_access_denied(self) -> None:
        device_resp = MagicMock(status_code=200)
        device_resp.json.return_value = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 1,
            "expires_in": 900,
        }
        denied_resp = MagicMock(status_code=200)
        denied_resp.json.return_value = {"error": "access_denied"}

        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = [device_resp, denied_resp]

        with patch.object(github_auth.httpx, "Client", return_value=client), patch.object(
            github_auth.time, "sleep", return_value=None
        ), patch.dict("os.environ", {"GITHUB_CLIENT_ID": "client-xyz"}):
            with self.assertRaises(github_auth.GitHubAuthError):
                github_auth.device_login(print_fn=lambda *_args: None)

    def test_device_login_without_client_id_raises(self) -> None:
        with patch.dict("os.environ", {"GITHUB_CLIENT_ID": ""}):
            with self.assertRaises(github_auth.GitHubAuthError):
                github_auth.device_login(print_fn=lambda *_args: None)


if __name__ == "__main__":
    unittest.main()
