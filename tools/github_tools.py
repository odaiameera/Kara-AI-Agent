"""GitHub tools for Kara, authenticated via OAuth Device Flow (github_auth.py).

Covers repos/files/code search, issues, pull requests, Actions runs, and
notifications read-only, plus approval-gated write actions (create/comment/
close issues, open/merge PRs, star) and git clone/pull/push using the OAuth
token as an ephemeral per-command credential (never written to git config).

STUDY GUIDE
-----------
* Wraps the GitHub REST API with httpx, reusing github_auth's stored token.
* Write actions and git push reuse computer_tools._approval_gate for the
  same two-turn "approve TOKEN" pattern as run_python_tests.
* Key concepts: REST pagination, JSON curation, subprocess + ephemeral auth.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

import auth_store
import config
import github_auth
from tools.file_tools import _resolve_path

API_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT = float(os.getenv("GITHUB_HTTP_TIMEOUT", "30"))
GIT_TIMEOUT_SECONDS = float(os.getenv("GITHUB_GIT_TIMEOUT", "120"))
MAX_LIST_ITEMS = 50
MAX_TEXT_CHARS = int(os.getenv("GITHUB_MAX_RESULT_CHARS", "20000"))

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_client: httpx.Client | None = None


class GitHubApiError(RuntimeError):
    """Raised for non-2xx GitHub API responses with a human-readable message."""


def _client_instance() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(base_url=API_BASE_URL, timeout=DEFAULT_TIMEOUT)
    return _client


def _not_ready_message() -> str:
    if not github_auth.has_credentials():
        return (
            "GitHub is not connected. Run: uv run python github_auth.py login "
            "(requires GITHUB_CLIENT_ID in personal_agent/.env from a GitHub OAuth App "
            "with Device Flow enabled — not a fine-grained personal access token)."
        )
    return ""


def _repo_slug(repo: str) -> str:
    """Validate an 'owner/name' repo reference and return it normalized."""
    repo = repo.strip()
    if not _REPO_RE.match(repo):
        raise ValueError(f"repo must look like 'owner/name', got '{repo}'.")
    return repo


def _one_of(value: str, allowed: set[str], field: str, default: str) -> str:
    """Normalize a small enum-ish argument, raising on anything unexpected."""
    chosen = value.strip().lower() or default
    if chosen not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}.")
    return chosen


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    creds = github_auth.runtime_credentials()
    accept = kwargs.pop("accept", "application/vnd.github+json")
    headers = {
        "Authorization": f"Bearer {creds['access_token']}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    headers.update(kwargs.pop("headers", None) or {})
    resp = _client_instance().request(method, path, headers=headers, **kwargs)
    if resp.status_code == 401:
        raise GitHubApiError(
            "GitHub token was rejected (401). Run `uv run python github_auth.py login` again."
        )
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        reset = resp.headers.get("X-RateLimit-Reset", "unknown")
        raise GitHubApiError(f"GitHub API rate limit exceeded. Resets at epoch {reset}.")
    if resp.status_code == 404:
        raise GitHubApiError("Not found (404) — check the repo/number/path and token access.")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str(resp.json().get("message", ""))
        except Exception:
            detail = resp.text[:300]
        raise GitHubApiError(f"GitHub API error {resp.status_code}: {detail or 'unknown error'}")
    return resp


def _clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 20
    return max(1, min(value, MAX_LIST_ITEMS))


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + f"\n... (truncated, {len(text) - MAX_TEXT_CHARS} more chars)"


def _json(payload: Any) -> str:
    return _truncate(json.dumps(payload, indent=2, ensure_ascii=False))


# LEARN: auth_store.AuthStoreError is the base of github_auth.GitHubAuthError, so
# listing it also covers a corrupt brain/auth.json surfacing mid-call.
_EXPECTED = (GitHubApiError, auth_store.AuthStoreError, ValueError, httpx.HTTPError, KeyError, TypeError)
# _run_git raises a bare RuntimeError when git itself is missing.
_EXPECTED_GIT = _EXPECTED + (RuntimeError, OSError, subprocess.SubprocessError)


def _github_tool(what: str, *, expected: tuple[type[BaseException], ...] = _EXPECTED):
    """Attach the shared "is GitHub connected?" guard and error envelope to a tool.

    Every public tool in this module needs the same two things, so they live
    here once instead of being re-typed 28 times.

    ``functools.wraps`` is load-bearing, not decoration: ``tool_schemas.function_to_tool``
    builds each LLM tool schema by introspecting ``inspect.signature``,
    ``get_type_hints``, ``__name__`` and the docstring. Without ``wraps`` (which
    also sets ``__wrapped__`` for ``signature`` to follow) every GitHub tool would
    silently degrade to a no-argument ``(*args, **kwargs)`` schema.
    """

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            not_ready = _not_ready_message()
            if not_ready:
                return not_ready
            try:
                return fn(*args, **kwargs)
            except expected as exc:
                return f"Error {what}: {exc}"

        return wrapper

    return decorate


# --- Read-only: account, repos, files, code -----------------------------------


@_github_tool("checking GitHub status")
def github_status() -> str:
    """
    Check whether Kara is connected to GitHub and show granted OAuth scopes.

    Returns:
        Connection status, scopes, and the authenticated login, or setup instructions.
    """
    resp = _request("GET", "/user")
    data = resp.json()
    creds = github_auth.runtime_credentials()
    return _json(
        {
            "connected": True,
            "login": data.get("login"),
            "name": data.get("name"),
            "scope": creds.get("scope") or "unknown",
            "rate_limit_remaining": resp.headers.get("X-RateLimit-Remaining"),
            "rate_limit_reset_epoch": resp.headers.get("X-RateLimit-Reset"),
        }
    )


@_github_tool("searching repositories")
def github_search_repositories(query: str, limit: int = 10) -> str:
    """
    Search GitHub repositories (public, plus private ones you can access).

    Args:
        query: GitHub search-repositories query, e.g. 'kara agent language:python'.
        limit: Max results to return (1-50).

    Returns:
        A list of matching repos with description, stars, and default branch.
    """
    query = query.strip()
    if not query:
        return "Error: query is required."
    resp = _request(
        "GET", "/search/repositories",
        params={"q": query, "per_page": _clamp_limit(limit)},
    )
    items = resp.json().get("items", [])
    if not items:
        return "No repositories found."
    results = [
        {
            "full_name": item.get("full_name"),
            "private": item.get("private"),
            "description": item.get("description"),
            "stars": item.get("stargazers_count"),
            "default_branch": item.get("default_branch"),
            "url": item.get("html_url"),
        }
        for item in items
    ]
    return _json(results)


@_github_tool("fetching repository")
def github_get_repository(repo: str) -> str:
    """
    Get metadata for a single GitHub repository.

    Args:
        repo: Repository as 'owner/name'.

    Returns:
        JSON with description, default branch, visibility, stars, open issues, topics.
    """
    slug = _repo_slug(repo)
    data = _request("GET", f"/repos/{slug}").json()
    return _json(
        {
            "full_name": data.get("full_name"),
            "private": data.get("private"),
            "description": data.get("description"),
            "default_branch": data.get("default_branch"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "language": data.get("language"),
            "archived": data.get("archived"),
            "topics": data.get("topics", []),
            "updated_at": data.get("updated_at"),
            "url": data.get("html_url"),
        }
    )


@_github_tool("listing repository contents")
def github_list_repository_contents(repo: str, path: str = "", ref: str = "") -> str:
    """
    List files and folders at a path in a repository (like 'ls' on GitHub).

    Args:
        repo: Repository as 'owner/name'.
        path: Directory path inside the repo (empty for repo root).
        ref: Branch, tag, or commit SHA (empty for the default branch).

    Returns:
        JSON list of entries with name, type (file/dir), size, and path.
    """
    slug = _repo_slug(repo)
    params = {"ref": ref.strip()} if ref.strip() else None
    data = _request("GET", f"/repos/{slug}/contents/{path.strip().lstrip('/')}", params=params).json()
    if isinstance(data, dict):
        data = [data]
    entries = [
        {"name": e.get("name"), "path": e.get("path"), "type": e.get("type"), "size": e.get("size")}
        for e in data
    ]
    return _json(entries)


@_github_tool("reading repository file")
def github_read_repository_file(repo: str, path: str, ref: str = "") -> str:
    """
    Read a text file's contents from a GitHub repository (no local clone needed).

    Args:
        repo: Repository as 'owner/name'.
        path: File path inside the repo.
        ref: Branch, tag, or commit SHA (empty for the default branch).

    Returns:
        The decoded file text, truncated if very large.
    """
    slug = _repo_slug(repo)
    params = {"ref": ref.strip()} if ref.strip() else None
    data = _request("GET", f"/repos/{slug}/contents/{path.strip().lstrip('/')}", params=params).json()
    if not isinstance(data, dict) or data.get("type") != "file":
        return f"Error: '{path}' is not a file in {repo}."
    if data.get("encoding") != "base64":
        return f"Error: unsupported encoding '{data.get('encoding')}' for this file."
    return _truncate(base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace"))


@_github_tool("searching code")
def github_search_code(query: str, repo: str = "", limit: int = 15) -> str:
    """
    Search code across GitHub, optionally scoped to one repository.

    Args:
        query: Code search query, e.g. 'def approval_gate'. Combine with repo for scope.
        repo: Optional 'owner/name' to restrict the search to one repository.
        limit: Max results to return (1-50).

    Returns:
        JSON list of matching files with repo, path, and URL.
    """
    query = query.strip()
    if not query:
        return "Error: query is required."
    if repo.strip():
        slug = _repo_slug(repo)
        query = f"{query} repo:{slug}"
    resp = _request("GET", "/search/code", params={"q": query, "per_page": _clamp_limit(limit)})
    items = resp.json().get("items", [])
    if not items:
        return "No code results found."
    results = [
        {"repo": item.get("repository", {}).get("full_name"), "path": item.get("path"), "url": item.get("html_url")}
        for item in items
    ]
    return _json(results)


@_github_tool("listing branches")
def github_list_branches(repo: str, limit: int = 30) -> str:
    """
    List branches in a repository.

    Args:
        repo: Repository as 'owner/name'.
        limit: Max branches to return (1-50).

    Returns:
        JSON list of branch names and whether each is protected.
    """
    slug = _repo_slug(repo)
    data = _request("GET", f"/repos/{slug}/branches", params={"per_page": _clamp_limit(limit)}).json()
    return _json([{"name": b.get("name"), "protected": b.get("protected")} for b in data])


@_github_tool("listing commits")
def github_list_commits(repo: str, branch: str = "", limit: int = 20) -> str:
    """
    List recent commits on a repository branch.

    Args:
        repo: Repository as 'owner/name'.
        branch: Branch name (empty for the default branch).
        limit: Max commits to return (1-50).

    Returns:
        JSON list of commits with SHA, author, date, and message summary.
    """
    slug = _repo_slug(repo)
    params: dict[str, Any] = {"per_page": _clamp_limit(limit)}
    if branch.strip():
        params["sha"] = branch.strip()
    data = _request("GET", f"/repos/{slug}/commits", params=params).json()
    results = [
        {
            "sha": c.get("sha", "")[:12],
            "author": (c.get("commit", {}).get("author", {}) or {}).get("name"),
            "date": (c.get("commit", {}).get("author", {}) or {}).get("date"),
            "message": (c.get("commit", {}).get("message", "") or "").splitlines()[0][:200],
        }
        for c in data
    ]
    return _json(results)


# --- Read-only: issues and pull requests ---------------------------------------


@_github_tool("listing issues")
def github_list_issues(repo: str, state: str = "open", limit: int = 20) -> str:
    """
    List issues in a repository (excludes pull requests).

    Args:
        repo: Repository as 'owner/name'.
        state: 'open', 'closed', or 'all'.
        limit: Max issues to return (1-50).

    Returns:
        JSON list of issue number, title, state, author, and comment count.
    """
    state = _one_of(state, {"open", "closed", "all"}, "state", "open")
    slug = _repo_slug(repo)
    data = _request(
        "GET", f"/repos/{slug}/issues",
        params={"state": state, "per_page": _clamp_limit(limit)},
    ).json()
    issues = [i for i in data if "pull_request" not in i]
    results = [
        {
            "number": i.get("number"),
            "title": i.get("title"),
            "state": i.get("state"),
            "author": (i.get("user") or {}).get("login"),
            "comments": i.get("comments"),
            "url": i.get("html_url"),
        }
        for i in issues
    ]
    return _json(results)


@_github_tool("fetching issue")
def github_get_issue(repo: str, number: int) -> str:
    """
    Get the full body and metadata of one issue.

    Args:
        repo: Repository as 'owner/name'.
        number: Issue number.

    Returns:
        JSON with title, state, author, labels, and body text.
    """
    slug = _repo_slug(repo)
    data = _request("GET", f"/repos/{slug}/issues/{int(number)}").json()
    return _json(
        {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "author": (data.get("user") or {}).get("login"),
            "labels": [l.get("name") for l in data.get("labels", [])],
            "comments": data.get("comments"),
            "body": (data.get("body") or "")[:MAX_TEXT_CHARS],
            "url": data.get("html_url"),
        }
    )


@_github_tool("listing issue comments")
def github_list_issue_comments(repo: str, number: int, limit: int = 20) -> str:
    """
    List comments on an issue or pull request.

    Args:
        repo: Repository as 'owner/name'.
        number: Issue or pull request number.
        limit: Max comments to return (1-50).

    Returns:
        JSON list of comments with author, date, and body text.
    """
    slug = _repo_slug(repo)
    data = _request(
        "GET", f"/repos/{slug}/issues/{int(number)}/comments",
        params={"per_page": _clamp_limit(limit)},
    ).json()
    results = [
        {
            "author": (c.get("user") or {}).get("login"),
            "created_at": c.get("created_at"),
            "body": (c.get("body") or "")[:2000],
        }
        for c in data
    ]
    return _json(results)


@_github_tool("searching issues")
def github_search_issues(query: str, limit: int = 20) -> str:
    """
    Search issues and pull requests across GitHub.

    Args:
        query: GitHub issues-search query, e.g. 'repo:owner/name is:open label:bug'.
        limit: Max results to return (1-50).

    Returns:
        JSON list of matching issues/PRs with repo, number, title, and state.
    """
    query = query.strip()
    if not query:
        return "Error: query is required."
    data = _request("GET", "/search/issues", params={"q": query, "per_page": _clamp_limit(limit)}).json()
    items = data.get("items", [])
    results = [
        {
            "repo": (i.get("repository_url") or "").rsplit("/repos/", 1)[-1],
            "number": i.get("number"),
            "title": i.get("title"),
            "state": i.get("state"),
            "is_pull_request": "pull_request" in i,
            "url": i.get("html_url"),
        }
        for i in items
    ]
    return _json(results)


@_github_tool("listing pull requests")
def github_list_pull_requests(repo: str, state: str = "open", limit: int = 20) -> str:
    """
    List pull requests in a repository.

    Args:
        repo: Repository as 'owner/name'.
        state: 'open', 'closed', or 'all'.
        limit: Max pull requests to return (1-50).

    Returns:
        JSON list of PR number, title, state, draft flag, and head/base branches.
    """
    state = _one_of(state, {"open", "closed", "all"}, "state", "open")
    slug = _repo_slug(repo)
    data = _request(
        "GET", f"/repos/{slug}/pulls",
        params={"state": state, "per_page": _clamp_limit(limit)},
    ).json()
    results = [
        {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "draft": pr.get("draft"),
            "head": (pr.get("head") or {}).get("ref"),
            "base": (pr.get("base") or {}).get("ref"),
            "author": (pr.get("user") or {}).get("login"),
            "url": pr.get("html_url"),
        }
        for pr in data
    ]
    return _json(results)


@_github_tool("fetching pull request")
def github_get_pull_request(repo: str, number: int) -> str:
    """
    Get metadata for one pull request (not the diff — use github_get_pull_request_diff for that).

    Args:
        repo: Repository as 'owner/name'.
        number: Pull request number.

    Returns:
        JSON with title, state, mergeable status, branches, and body text.
    """
    slug = _repo_slug(repo)
    data = _request("GET", f"/repos/{slug}/pulls/{int(number)}").json()
    return _json(
        {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "draft": data.get("draft"),
            "mergeable": data.get("mergeable"),
            "mergeable_state": data.get("mergeable_state"),
            "head": (data.get("head") or {}).get("ref"),
            "base": (data.get("base") or {}).get("ref"),
            "additions": data.get("additions"),
            "deletions": data.get("deletions"),
            "changed_files": data.get("changed_files"),
            "body": (data.get("body") or "")[:MAX_TEXT_CHARS],
            "url": data.get("html_url"),
        }
    )


@_github_tool("fetching pull request diff")
def github_get_pull_request_diff(repo: str, number: int) -> str:
    """
    Get the unified diff for a pull request.

    Args:
        repo: Repository as 'owner/name'.
        number: Pull request number.

    Returns:
        Raw unified diff text, truncated if very large.
    """
    slug = _repo_slug(repo)
    resp = _request(
        "GET", f"/repos/{slug}/pulls/{int(number)}",
        accept="application/vnd.github.v3.diff",
    )
    text = resp.text
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n... (truncated, {len(text) - MAX_TEXT_CHARS} more chars)"
    return text or "(empty diff)"


@_github_tool("listing pull request files")
def github_list_pull_request_files(repo: str, number: int, limit: int = 30) -> str:
    """
    List the files changed by a pull request.

    Args:
        repo: Repository as 'owner/name'.
        number: Pull request number.
        limit: Max files to return (1-50).

    Returns:
        JSON list of filename, status (added/modified/removed), and line counts.
    """
    slug = _repo_slug(repo)
    data = _request(
        "GET", f"/repos/{slug}/pulls/{int(number)}/files",
        params={"per_page": _clamp_limit(limit)},
    ).json()
    results = [
        {
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
        }
        for f in data
    ]
    return _json(results)


# --- Read-only: Actions and notifications --------------------------------------


@_github_tool("listing workflow runs")
def github_list_workflow_runs(repo: str, limit: int = 10) -> str:
    """
    List recent GitHub Actions workflow runs for a repository.

    Args:
        repo: Repository as 'owner/name'.
        limit: Max runs to return (1-50).

    Returns:
        JSON list of run id, workflow name, status, conclusion, and branch.
    """
    slug = _repo_slug(repo)
    data = _request(
        "GET", f"/repos/{slug}/actions/runs",
        params={"per_page": _clamp_limit(limit)},
    ).json()
    runs = data.get("workflow_runs", [])
    results = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "branch": r.get("head_branch"),
            "event": r.get("event"),
            "url": r.get("html_url"),
        }
        for r in runs
    ]
    return _json(results)


@_github_tool("fetching workflow run")
def github_get_workflow_run(repo: str, run_id: int) -> str:
    """
    Get details for one GitHub Actions workflow run.

    Args:
        repo: Repository as 'owner/name'.
        run_id: Workflow run id (from github_list_workflow_runs).

    Returns:
        JSON with status, conclusion, timing, and the run's jobs URL.
    """
    slug = _repo_slug(repo)
    data = _request("GET", f"/repos/{slug}/actions/runs/{int(run_id)}").json()
    return _json(
        {
            "id": data.get("id"),
            "name": data.get("name"),
            "status": data.get("status"),
            "conclusion": data.get("conclusion"),
            "branch": data.get("head_branch"),
            "run_started_at": data.get("run_started_at"),
            "updated_at": data.get("updated_at"),
            "url": data.get("html_url"),
        }
    )


@_github_tool("listing notifications")
def github_list_notifications(limit: int = 20, unread_only: bool = True) -> str:
    """
    List GitHub notifications for the authenticated user.

    Args:
        limit: Max notifications to return (1-50).
        unread_only: If True, only unread notifications (default). If False, includes read ones too.

    Returns:
        JSON list of reason, repo, subject title/type, and updated time.
    """
    data = _request(
        "GET", "/notifications",
        params={"per_page": _clamp_limit(limit), "all": not unread_only},
    ).json()
    results = [
        {
            "repo": (n.get("repository") or {}).get("full_name"),
            "reason": n.get("reason"),
            "type": (n.get("subject") or {}).get("type"),
            "title": (n.get("subject") or {}).get("title"),
            "unread": n.get("unread"),
            "updated_at": n.get("updated_at"),
        }
        for n in data
    ]
    return _json(results)


# --- Write actions (approval-gated) --------------------------------------------


def _approval(action: str, intent: dict[str, Any], summary: str, approval_token: str) -> str | None:
    from tools import computer_tools

    target = {"pid": 0, "window_id": 0, "title": intent.get("repo", "github"), "app_name": "GitHub API"}
    return computer_tools._approval_gate(action, intent, target, approval_token, summary)


@_github_tool("creating issue")
def github_create_issue(repo: str, title: str, body: str = "", approval_token: str = "") -> str:
    """
    Create a new issue in a repository. Requires the user's exact two-turn approval.

    Args:
        repo: Repository as 'owner/name'.
        title: Issue title.
        body: Issue body text (Markdown).
        approval_token: Token supplied only after the user replies exactly 'approve TOKEN'.

    Returns:
        Approval request, or the created issue's number and URL.
    """
    title = title.strip()
    if not title:
        return "Error: title is required."
    slug = _repo_slug(repo)
    intent = {"repo": repo, "title": title, "body": body}
    approval = _approval(
        "github_create_issue", intent,
        f"create a GitHub issue titled '{title}' in {repo}", approval_token,
    )
    if approval:
        return approval
    data = _request(
        "POST", f"/repos/{slug}/issues", json={"title": title, "body": body},
    ).json()
    return _json({"number": data.get("number"), "url": data.get("html_url")})


@_github_tool("commenting on issue")
def github_comment_on_issue(repo: str, number: int, body: str, approval_token: str = "") -> str:
    """
    Post a comment on an issue or pull request. Requires the user's exact two-turn approval.

    Args:
        repo: Repository as 'owner/name'.
        number: Issue or pull request number.
        body: Comment text (Markdown).
        approval_token: Token supplied only after the user replies exactly 'approve TOKEN'.

    Returns:
        Approval request, or the created comment's URL.
    """
    body = body.strip()
    if not body:
        return "Error: body is required."
    slug = _repo_slug(repo)
    intent = {"repo": repo, "number": int(number), "body": body}
    approval = _approval(
        "github_comment_on_issue", intent,
        f"post a comment on {repo}#{int(number)}", approval_token,
    )
    if approval:
        return approval
    data = _request(
        "POST", f"/repos/{slug}/issues/{int(number)}/comments", json={"body": body},
    ).json()
    return _json({"url": data.get("html_url")})


@_github_tool("closing issue")
def github_close_issue(repo: str, number: int, approval_token: str = "") -> str:
    """
    Close an issue. Requires the user's exact two-turn approval.

    Args:
        repo: Repository as 'owner/name'.
        number: Issue number.
        approval_token: Token supplied only after the user replies exactly 'approve TOKEN'.

    Returns:
        Approval request, or confirmation the issue was closed.
    """
    slug = _repo_slug(repo)
    intent = {"repo": repo, "number": int(number)}
    approval = _approval(
        "github_close_issue", intent, f"close issue {repo}#{int(number)}", approval_token,
    )
    if approval:
        return approval
    data = _request(
        "PATCH", f"/repos/{slug}/issues/{int(number)}", json={"state": "closed"},
    ).json()
    return _json({"number": data.get("number"), "state": data.get("state")})


@_github_tool("creating pull request")
def github_create_pull_request(
    repo: str, title: str, head: str, base: str, body: str = "", draft: bool = False, approval_token: str = "",
) -> str:
    """
    Open a pull request. Requires the user's exact two-turn approval.

    Args:
        repo: Repository as 'owner/name'.
        title: Pull request title.
        head: Source branch (or 'user:branch' for a fork).
        base: Target branch to merge into.
        body: Pull request description (Markdown).
        draft: Open as a draft pull request.
        approval_token: Token supplied only after the user replies exactly 'approve TOKEN'.

    Returns:
        Approval request, or the created PR's number and URL.
    """
    title, head, base = title.strip(), head.strip(), base.strip()
    if not title or not head or not base:
        return "Error: title, head, and base are all required."
    slug = _repo_slug(repo)
    intent = {"repo": repo, "title": title, "head": head, "base": base, "draft": bool(draft)}
    approval = _approval(
        "github_create_pull_request", intent,
        f"open a pull request '{title}' ({head} -> {base}) in {repo}", approval_token,
    )
    if approval:
        return approval
    data = _request(
        "POST", f"/repos/{slug}/pulls",
        json={"title": title, "head": head, "base": base, "body": body, "draft": bool(draft)},
    ).json()
    return _json({"number": data.get("number"), "url": data.get("html_url")})


@_github_tool("merging pull request")
def github_merge_pull_request(
    repo: str, number: int, merge_method: str = "merge", approval_token: str = "",
) -> str:
    """
    Merge a pull request. Requires the user's exact two-turn approval.

    Args:
        repo: Repository as 'owner/name'.
        number: Pull request number.
        merge_method: 'merge', 'squash', or 'rebase'.
        approval_token: Token supplied only after the user replies exactly 'approve TOKEN'.

    Returns:
        Approval request, or confirmation the PR was merged.
    """
    merge_method = _one_of(merge_method, {"merge", "squash", "rebase"}, "merge_method", "merge")
    slug = _repo_slug(repo)
    intent = {"repo": repo, "number": int(number), "merge_method": merge_method}
    approval = _approval(
        "github_merge_pull_request", intent,
        f"merge pull request {repo}#{int(number)} ({merge_method})", approval_token,
    )
    if approval:
        return approval
    data = _request(
        "PUT", f"/repos/{slug}/pulls/{int(number)}/merge",
        json={"merge_method": merge_method},
    ).json()
    return _json({"merged": data.get("merged"), "message": data.get("message")})


@_github_tool("starring repository")
def github_star_repository(repo: str, approval_token: str = "") -> str:
    """
    Star a repository on GitHub (public action visible on your profile). Requires
    the user's exact two-turn approval.

    Args:
        repo: Repository as 'owner/name'.
        approval_token: Token supplied only after the user replies exactly 'approve TOKEN'.

    Returns:
        Approval request, or confirmation the repo was starred.
    """
    slug = _repo_slug(repo)
    intent = {"repo": repo}
    approval = _approval("github_star_repository", intent, f"star {repo} on GitHub", approval_token)
    if approval:
        return approval
    _request("PUT", f"/user/starred/{slug}")
    return f"Starred {repo}."


# --- Git (clone/pull/push via ephemeral OAuth credential) ----------------------


def _run_git(args: list[str], *, cwd: Path | None, token: str) -> tuple[int, str, str]:
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError("git is not installed or not on PATH.")
    cmd = [git_bin, "-c", f"http.extraheader=AUTHORIZATION: bearer {token}", *args]
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _require_git_repo(path: Path) -> None:
    if not (path / ".git").exists():
        raise ValueError(f"{path} is not a git repository (no .git directory).")


@_github_tool("cloning repository", expected=_EXPECTED_GIT)
def git_clone_repository(repo: str, dest_path: str, ref: str = "") -> str:
    """
    Clone a GitHub repository (private or public) to a local directory using
    the connected OAuth token, without ever writing the token to disk.

    Args:
        repo: Repository as 'owner/name'.
        dest_path: Destination directory inside an allowed write root; must not already exist.
        ref: Branch or tag to check out (empty for the default branch).

    Returns:
        Confirmation with the local path, or an error message.
    """
    slug = _repo_slug(repo)
    dest = _resolve_path(dest_path, config.FILE_WRITE_ROOTS, purpose="write")
    if dest.exists() and any(dest.iterdir()):
        return f"Error: destination '{dest}' already exists and is not empty."
    creds = github_auth.runtime_credentials()
    url = f"https://github.com/{slug}.git"
    args = ["clone", url, str(dest)]
    if ref.strip():
        args.extend(["--branch", ref.strip()])
    code, out, git_err = _run_git(args, cwd=None, token=creds["access_token"])
    if code != 0:
        return f"Error cloning {repo}: {git_err or out or 'unknown git error'}"
    return f"Cloned {repo} to {dest}."


@_github_tool("pulling repository", expected=_EXPECTED_GIT)
def git_pull_repository(repo_path: str) -> str:
    """
    Fast-forward pull the latest changes into an existing local clone.

    Args:
        repo_path: Path to an existing local git repository inside an allowed write root.

    Returns:
        Git's pull output, or an error message.
    """
    path = _resolve_path(repo_path, config.FILE_WRITE_ROOTS, purpose="write")
    _require_git_repo(path)
    creds = github_auth.runtime_credentials()
    code, out, git_err = _run_git(["pull", "--ff-only"], cwd=path, token=creds["access_token"])
    if code != 0:
        return f"Error pulling {path}: {git_err or out or 'unknown git error'}"
    return out or git_err or "Already up to date."


@_github_tool("pushing changes", expected=_EXPECTED_GIT)
def git_push_changes(
    repo_path: str, message: str, branch: str = "", create_branch: bool = False, approval_token: str = "",
) -> str:
    """
    Stage all changes, commit, and push to GitHub. Requires the user's exact
    two-turn approval (this publishes changes to a shared remote).

    Args:
        repo_path: Path to an existing local git repository inside an allowed write root.
        message: Commit message.
        branch: Branch to push to (empty to use the current branch).
        create_branch: Create and switch to 'branch' before committing (requires branch).
        approval_token: Token supplied only after the user replies exactly 'approve TOKEN'.

    Returns:
        Approval request, or a summary of the commit and push result.
    """
    message = message.strip()
    if not message:
        return "Error: message is required."
    if create_branch and not branch.strip():
        return "Error: create_branch requires a branch name."
    path = _resolve_path(repo_path, config.FILE_WRITE_ROOTS, purpose="write")
    _require_git_repo(path)
    intent = {
        "repo_path": str(path), "message": message,
        "branch": branch.strip(), "create_branch": bool(create_branch),
    }
    approval = _approval(
        "git_push_changes", intent,
        f"commit and push changes in '{path}'" + (f" to branch '{branch.strip()}'" if branch.strip() else ""),
        approval_token,
    )
    if approval:
        return approval

    creds = github_auth.runtime_credentials()
    token = creds["access_token"]
    steps: list[dict[str, Any]] = []

    if create_branch:
        code, out, git_err = _run_git(["checkout", "-b", branch.strip()], cwd=path, token=token)
        steps.append({"step": "checkout -b", "ok": code == 0, "output": git_err or out})
        if code != 0:
            return _json({"ok": False, "steps": steps})
    elif branch.strip():
        code, out, git_err = _run_git(["checkout", branch.strip()], cwd=path, token=token)
        steps.append({"step": "checkout", "ok": code == 0, "output": git_err or out})
        if code != 0:
            return _json({"ok": False, "steps": steps})

    code, out, git_err = _run_git(["add", "-A"], cwd=path, token=token)
    steps.append({"step": "add -A", "ok": code == 0, "output": git_err or out})
    if code != 0:
        return _json({"ok": False, "steps": steps})

    code, out, git_err = _run_git(["commit", "-m", message], cwd=path, token=token)
    nothing_to_commit = "nothing to commit" in (out + git_err).lower()
    steps.append({"step": "commit", "ok": code == 0 or nothing_to_commit, "output": git_err or out})
    if code != 0 and not nothing_to_commit:
        return _json({"ok": False, "steps": steps})

    push_args = ["push", "-u", "origin", "HEAD"]
    code, out, git_err = _run_git(push_args, cwd=path, token=token)
    steps.append({"step": "push", "ok": code == 0, "output": git_err or out})
    return _json({"ok": code == 0, "steps": steps})
