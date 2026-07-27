from __future__ import annotations

import unittest

import kara


class GitHubToolRegistrationTests(unittest.TestCase):
    def test_github_tools_are_registered_and_schema_visible(self) -> None:
        expected = {
            "github_status",
            "github_search_repositories",
            "github_get_repository",
            "github_list_repository_contents",
            "github_read_repository_file",
            "github_search_code",
            "github_list_branches",
            "github_list_commits",
            "github_list_issues",
            "github_get_issue",
            "github_list_issue_comments",
            "github_search_issues",
            "github_list_pull_requests",
            "github_get_pull_request",
            "github_get_pull_request_diff",
            "github_list_pull_request_files",
            "github_list_workflow_runs",
            "github_get_workflow_run",
            "github_list_notifications",
            "github_create_issue",
            "github_comment_on_issue",
            "github_close_issue",
            "github_create_pull_request",
            "github_merge_pull_request",
            "github_star_repository",
            "git_clone_repository",
            "git_pull_repository",
            "git_push_changes",
        }
        schema_names = {item["function"]["name"] for item in kara.TOOL_SCHEMAS}

        self.assertTrue(expected.issubset(kara.TOOL_REGISTRY))
        self.assertTrue(expected.issubset(schema_names))


if __name__ == "__main__":
    unittest.main()
