from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import github_auth
from tools import computer_tools, github_tools


def _fake_response(payload, headers: dict | None = None, text: str = ""):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.text = text
    resp.headers = headers or {}
    return resp


class RepoValidationTests(unittest.TestCase):
    def test_repo_slug_accepts_and_normalizes_owner_slash_name(self) -> None:
        self.assertEqual(github_tools._repo_slug("  odaiameera/Kara-local "), "odaiameera/Kara-local")

    def test_repo_slug_rejects_missing_slash(self) -> None:
        with self.assertRaises(ValueError):
            github_tools._repo_slug("not-a-repo")

    def test_not_ready_message_when_disconnected(self) -> None:
        with patch.object(github_tools.github_auth, "has_credentials", return_value=False):
            self.assertIn("login", github_tools._not_ready_message().lower())

    def test_not_ready_message_empty_when_connected(self) -> None:
        with patch.object(github_tools.github_auth, "has_credentials", return_value=True):
            self.assertEqual(github_tools._not_ready_message(), "")


class ReadOnlyToolTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(github_tools.github_auth, "has_credentials", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_github_get_repository_curates_fields(self) -> None:
        payload = {
            "full_name": "odaiameera/Kara-local",
            "private": True,
            "description": "Kara agent",
            "default_branch": "main",
            "stargazers_count": 3,
            "forks_count": 1,
            "open_issues_count": 2,
            "language": "Python",
            "archived": False,
            "topics": ["agent"],
            "updated_at": "2026-07-01T00:00:00Z",
            "html_url": "https://github.com/odaiameera/Kara-local",
        }
        with patch.object(github_tools, "_request", return_value=_fake_response(payload)) as request_mock:
            result = json.loads(github_tools.github_get_repository("odaiameera/Kara-local"))

        request_mock.assert_called_once_with("GET", "/repos/odaiameera/Kara-local")
        self.assertEqual(result["full_name"], "odaiameera/Kara-local")
        self.assertEqual(result["stars"], 3)
        self.assertTrue(result["private"])

    def test_github_get_repository_rejects_bad_repo_format(self) -> None:
        result = github_tools.github_get_repository("not-a-repo")
        self.assertIn("Error", result)

    def test_github_list_issues_filters_out_pull_requests(self) -> None:
        payload = [
            {"number": 1, "title": "Real issue", "state": "open", "user": {"login": "odai"}, "comments": 0, "html_url": "u1"},
            {"number": 2, "title": "A PR", "state": "open", "pull_request": {}, "user": {"login": "odai"}, "comments": 0, "html_url": "u2"},
        ]
        with patch.object(github_tools, "_request", return_value=_fake_response(payload)):
            result = json.loads(github_tools.github_list_issues("odaiameera/Kara-local"))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 1)

    def test_github_status_reports_not_ready_when_disconnected(self) -> None:
        with patch.object(github_tools.github_auth, "has_credentials", return_value=False):
            result = github_tools.github_status()
        self.assertIn("login", result.lower())

    def test_request_raises_on_401(self) -> None:
        client = MagicMock()
        client.request.return_value = MagicMock(status_code=401, headers={})
        with patch.object(github_tools, "_client_instance", return_value=client), patch.object(
            github_tools.github_auth, "runtime_credentials", return_value={"access_token": "tok", "scope": "repo"}
        ):
            with self.assertRaises(github_tools.GitHubApiError):
                github_tools._request("GET", "/user")

    def test_request_raises_on_rate_limit(self) -> None:
        client = MagicMock()
        client.request.return_value = MagicMock(
            status_code=403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"}
        )
        with patch.object(github_tools, "_client_instance", return_value=client), patch.object(
            github_tools.github_auth, "runtime_credentials", return_value={"access_token": "tok", "scope": "repo"}
        ):
            with self.assertRaises(github_tools.GitHubApiError) as ctx:
                github_tools._request("GET", "/user")
        self.assertIn("rate limit", str(ctx.exception).lower())


class WriteActionApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        computer_tools._pending.clear()
        patcher = patch.object(github_tools.github_auth, "has_credentials", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_github_create_issue_requires_exact_later_user_approval(self) -> None:
        computer_tools.set_computer_request_context("gh-session", "file the bug")
        with patch.object(github_tools, "_request") as request_mock:
            requested = json.loads(github_tools.github_create_issue("odaiameera/Kara-local", "Bug title"))
            token = requested["approval"]["token"]

            same_turn = json.loads(
                github_tools.github_create_issue("odaiameera/Kara-local", "Bug title", approval_token=token)
            )

            computer_tools.set_computer_request_context("gh-session", f"approve {token}")
            request_mock.return_value = _fake_response(
                {"number": 42, "html_url": "https://github.com/odaiameera/Kara-local/issues/42"}
            )
            completed = json.loads(
                github_tools.github_create_issue("odaiameera/Kara-local", "Bug title", approval_token=token)
            )

        self.assertEqual(requested["status"], "approval_required")
        self.assertEqual(same_turn["error"]["code"], "approval_not_confirmed")
        self.assertEqual(completed["number"], 42)
        request_mock.assert_called_once()

    def test_github_merge_pull_request_rejects_bad_merge_method_before_approval(self) -> None:
        result = github_tools.github_merge_pull_request("odaiameera/Kara-local", 5, merge_method="explode")
        self.assertIn("merge_method", result)


class GitSubprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        computer_tools._pending.clear()
        patcher = patch.object(github_tools.github_auth, "has_credentials", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        creds_patcher = patch.object(
            github_tools.github_auth, "runtime_credentials",
            return_value={"access_token": "tok-secret", "scope": "repo"},
        )
        creds_patcher.start()
        self.addCleanup(creds_patcher.stop)

    def test_git_subprocess_uses_oauth_without_interactive_credential_manager(self) -> None:
        environment = github_tools._git_environment("tok-secret")

        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GCM_INTERACTIVE"], "Never")
        self.assertEqual(environment["KARA_GIT_OAUTH_TOKEN"], "tok-secret")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(environment["GIT_CONFIG_KEY_1"], "credential.helper")
        helper = environment["GIT_CONFIG_VALUE_1"]
        self.assertIn("username=x-access-token", helper)
        self.assertIn("$KARA_GIT_OAUTH_TOKEN", helper)
        self.assertNotIn("tok-secret", helper)

        with patch.dict(os.environ, {"KARA_GIT_OAUTH_TOKEN": "inherited-secret"}):
            local_environment = github_tools._git_environment()
        self.assertNotIn("KARA_GIT_OAUTH_TOKEN", local_environment)

    def test_git_timeout_terminates_the_process_tree(self) -> None:
        process = MagicMock(pid=4242)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="git", timeout=1),
            ("", ""),
        ]
        with patch.object(github_tools.shutil, "which", return_value="git.exe"), patch.object(
            github_tools.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1),
        ), patch.object(
            github_tools.subprocess, "Popen", return_value=process
        ), patch.object(
            github_tools, "_assign_windows_git_job", return_value=99, create=True
        ), patch.object(
            github_tools, "_close_windows_job", create=True
        ) as close_job, patch.object(
            github_tools, "_terminate_git_process_tree", create=True
        ) as terminate_tree:
            with self.assertRaises(subprocess.TimeoutExpired):
                github_tools._run_git(["push"], cwd=None, token="tok-secret")

        terminate_tree.assert_called_once_with(process)
        close_job.assert_any_call(99)

    def test_git_subprocess_keeps_token_off_command_line_and_redacts_output(self) -> None:
        process = MagicMock(pid=4242, returncode=1)
        process.communicate.return_value = (
            "stdout accidentally contained tok-secret",
            "stderr accidentally contained tok-secret",
        )
        with patch.object(github_tools.shutil, "which", return_value="git.exe"), patch.object(
            github_tools.subprocess, "Popen", return_value=process
        ) as popen, patch.object(
            github_tools, "_assign_windows_git_job", return_value=99
        ), patch.object(
            github_tools, "_close_windows_job"
        ) as close_job:
            code, out, err = github_tools._run_git(["push"], cwd=None, token="tok-secret")

        self.assertEqual(code, 1)
        self.assertNotIn("tok-secret", " ".join(popen.call_args.args[0]))
        self.assertNotIn("tok-secret", out + err)
        self.assertIn("[REDACTED]", out)
        self.assertIn("[REDACTED]", err)
        close_job.assert_called_once_with(99)

    def test_git_clone_repository_passes_ephemeral_oauth_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            dest = root / "clone-target"
            with patch.object(github_tools.config, "FILE_WRITE_ROOTS", (root,)), patch.object(
                github_tools, "_run_git", return_value=(0, "Cloning into 'clone-target'...", "")
            ) as run_git:
                result = github_tools.git_clone_repository("odaiameera/Kara-local", str(dest))

        self.assertIn("Cloned", result)
        args, kwargs = run_git.call_args
        git_args = args[0]
        self.assertIn("clone", git_args)
        self.assertIn("https://github.com/odaiameera/Kara-local.git", git_args)
        self.assertEqual(kwargs["token"], "tok-secret")

    def test_git_clone_repository_refuses_non_empty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            dest = root / "clone-target"
            dest.mkdir()
            (dest / "existing.txt").write_text("already here", encoding="utf-8")
            with patch.object(github_tools.config, "FILE_WRITE_ROOTS", (root,)):
                result = github_tools.git_clone_repository("odaiameera/Kara-local", str(dest))
        self.assertIn("Error", result)
        self.assertIn("not empty", result)

    def test_git_push_changes_requires_exact_later_user_approval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / ".git").mkdir()
            computer_tools.set_computer_request_context("gh-session", "push my changes")

            with patch.object(github_tools.config, "FILE_WRITE_ROOTS", (root,)), patch.object(
                github_tools, "_run_git"
            ) as run_git:
                requested = json.loads(github_tools.git_push_changes(str(root), "fix bug"))
                token = requested["approval"]["token"]

                same_turn = json.loads(
                    github_tools.git_push_changes(str(root), "fix bug", approval_token=token)
                )

                computer_tools.set_computer_request_context("gh-session", f"approve {token}")
                run_git.side_effect = [
                    (0, "", ""),  # add -A
                    (0, "[main abc1234] fix bug", ""),  # commit
                    (0, "branch 'main' set up to track 'origin/main'.", ""),  # push
                ]
                completed = json.loads(
                    github_tools.git_push_changes(str(root), "fix bug", approval_token=token)
                )

        self.assertEqual(requested["status"], "approval_required")
        self.assertEqual(same_turn["error"]["code"], "approval_not_confirmed")
        self.assertTrue(completed["ok"])
        self.assertEqual(len(completed["steps"]), 3)
        self.assertEqual(run_git.call_count, 3)
        self.assertNotIn("token", run_git.call_args_list[0].kwargs)
        self.assertNotIn("token", run_git.call_args_list[1].kwargs)
        self.assertEqual(run_git.call_args_list[2].kwargs["token"], "tok-secret")


if __name__ == "__main__":
    unittest.main()
