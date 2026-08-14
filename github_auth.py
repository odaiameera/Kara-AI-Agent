"""GitHub OAuth for Kara (OAuth App device-code flow, not a fine-grained token).

STUDY GUIDE
-----------
* Runs GitHub's OAuth Device Flow and stores tokens in ``brain/auth.json``.
* Refreshes access tokens when GitHub issues expiring user tokens (optional
  app setting); classic device-flow tokens have no expiry and are reused as-is.
* Key concepts: OAuth device flow, JSON token storage, polling with backoff.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Callable, TextIO

import httpx

import auth_store
from auth_store import auth_file  # re-exported: callers use github_auth.auth_file()

GITHUB_PROVIDER_ID = "github"
GITHUB_CLIENT_ID_ENV = "GITHUB_CLIENT_ID"
GITHUB_SCOPES_ENV = "GITHUB_OAUTH_SCOPES"
DEFAULT_SCOPES = "repo workflow gist read:org notifications"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
VERIFY_URL = "https://github.com/login/device"
REFRESH_SKEW_SECONDS = 120


class GitHubAuthError(auth_store.AuthStoreError):
    """Raised when GitHub OAuth state is missing, invalid, or misconfigured."""


def _client_id() -> str:
    client_id = os.getenv(GITHUB_CLIENT_ID_ENV, "").strip()
    if not client_id:
        raise GitHubAuthError(
            f"{GITHUB_CLIENT_ID_ENV} is not set in .env. "
            "Create a GitHub OAuth App (Settings -> Developer settings -> OAuth Apps), "
            "enable Device Flow on it, and paste the Client ID into .env."
        )
    return client_id


def _scopes() -> str:
    return os.getenv(GITHUB_SCOPES_ENV, "").strip() or DEFAULT_SCOPES


def save_tokens(tokens: dict[str, Any], *, scope: str = "") -> None:
    """Persist GitHub OAuth tokens to ``brain/auth.json``.

    Tokens are intentionally not stored in ``.env`` and should never be printed.
    """
    access_token = str(tokens.get("access_token", "") or "").strip()
    if not access_token:
        raise GitHubAuthError("GitHub token response did not include access_token.")
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    expires_in = tokens.get("expires_in")
    refresh_expires_in = tokens.get("refresh_token_expires_in")
    now = time.time()

    entry: dict[str, Any] = {
        "auth_mode": "oauth-device",
        "scope": scope or str(tokens.get("scope", "") or ""),
        "last_refresh": auth_store.now_iso(),
        "tokens": {
            "access_token": access_token,
        },
    }
    if refresh_token:
        entry["tokens"]["refresh_token"] = refresh_token
    if isinstance(expires_in, (int, float)):
        entry["access_token_expires_at"] = now + float(expires_in)
    if isinstance(refresh_expires_in, (int, float)):
        entry["refresh_token_expires_at"] = now + float(refresh_expires_in)
    auth_store.write_provider(GITHUB_PROVIDER_ID, entry)


def read_tokens() -> dict[str, Any]:
    """Read stored GitHub tokens or raise a clear re-login error."""
    state = auth_store.read_provider(GITHUB_PROVIDER_ID)
    if state is None:
        raise GitHubAuthError(
            "No GitHub credentials stored. Run `uv run python github_auth.py login`."
        )
    tokens = state.get("tokens")
    if not isinstance(tokens, dict) or not str(tokens.get("access_token", "") or "").strip():
        raise GitHubAuthError("GitHub auth state is incomplete; login again.")
    return state


def access_token_is_expiring(state: dict[str, Any], skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    """Only classic-OAuth (non-expiring) tokens are the common case; expiring
    user tokens are an opt-in GitHub App setting, so this is a no-op unless
    ``access_token_expires_at`` was actually recorded at login/refresh time."""
    expires_at = state.get("access_token_expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    return float(expires_at) <= time.time() + skew_seconds


def refresh_tokens() -> dict[str, str]:
    """Refresh and persist the GitHub access token (only applies to apps with
    'expiring user authorization tokens' enabled; raises otherwise)."""
    state = read_tokens()
    tokens = state["tokens"]
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not refresh_token:
        raise GitHubAuthError(
            "This GitHub OAuth App does not issue refresh tokens (expiring tokens "
            "not enabled), so there is nothing to refresh. The access token does not expire."
        )
    with httpx.Client(timeout=httpx.Timeout(20.0), headers={"Accept": "application/json"}) as client:
        response = client.post(
            ACCESS_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _client_id(),
            },
        )
    if response.status_code != 200:
        raise GitHubAuthError(
            f"GitHub token refresh failed with HTTP {response.status_code}; login again if it persists."
        )
    payload = response.json()
    if payload.get("error"):
        raise GitHubAuthError(
            f"GitHub token refresh failed: {payload.get('error_description') or payload['error']}"
        )
    save_tokens(payload, scope=str(state.get("scope", "") or ""))
    updated = read_tokens()
    return dict(updated["tokens"])


def runtime_credentials(*, refresh_if_expiring: bool = True) -> dict[str, str]:
    """Return a fresh bearer token for GitHub API/git calls."""
    state = read_tokens()
    if refresh_if_expiring and access_token_is_expiring(state) and state["tokens"].get("refresh_token"):
        tokens = refresh_tokens()
    else:
        tokens = dict(state["tokens"])
    return {
        "access_token": str(tokens.get("access_token", "") or "").strip(),
        "scope": str(state.get("scope", "") or ""),
    }


def git_credential_helper(
    operation: str,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> int:
    """Serve Git credentials only for HTTPS requests to github.com.

    Git launches this trusted helper as a separate process.  The token is read
    from Kara's auth store here rather than inherited by Git, so repository
    hooks, filters, aliases, and remote helpers never receive it in their
    environment.
    """
    if operation in {"store", "erase"}:
        return 0
    if operation != "get":
        return 1

    source = input_stream if input_stream is not None else sys.stdin
    destination = output_stream if output_stream is not None else sys.stdout
    errors = error_stream if error_stream is not None else sys.stderr
    request: dict[str, str] = {}
    for raw_line in source:
        line = raw_line.rstrip("\r\n")
        if not line:
            break
        key, separator, value = line.partition("=")
        if separator:
            request[key] = value

    if request.get("protocol", "").lower() != "https" or request.get("host", "").lower() != "github.com":
        return 0
    try:
        credentials = runtime_credentials(refresh_if_expiring=False)
    except auth_store.AuthStoreError as exc:
        print(f"GitHub credentials unavailable: {exc}", file=errors)
        return 1
    token = credentials.get("access_token", "").strip()
    if not token:
        print("GitHub credentials unavailable: access token is empty.", file=errors)
        return 1
    print("username=x-access-token", file=destination)
    print(f"password={token}", file=destination)
    return 0


def has_credentials() -> bool:
    # LEARN: catch the shared base — a corrupt auth.json raises AuthStoreError
    # from auth_store, which is NOT a GitHubAuthError.
    try:
        read_tokens()
        return True
    except auth_store.AuthStoreError:
        return False


def device_login(*, print_fn: Callable[[str], None] = print) -> dict[str, Any]:
    """Run GitHub's OAuth Device Flow and return the token payload.

    This function performs network I/O and waits for the user to complete
    browser login/approval.
    """
    client_id = _client_id()
    scope = _scopes()
    with httpx.Client(timeout=httpx.Timeout(20.0), headers={"Accept": "application/json"}) as client:
        device_resp = client.post(
            DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": scope},
        )
        if device_resp.status_code != 200:
            raise GitHubAuthError(f"Device code request failed with HTTP {device_resp.status_code}.")
        device_data = device_resp.json()
        device_code = str(device_data.get("device_code", "") or "").strip()
        user_code = str(device_data.get("user_code", "") or "").strip()
        verification_uri = str(device_data.get("verification_uri", "") or VERIFY_URL).strip()
        poll_interval = max(1, int(device_data.get("interval") or 5))
        expires_in = int(device_data.get("expires_in") or 900)
        if not device_code or not user_code:
            raise GitHubAuthError("Device code response was missing device_code/user_code.")

        print_fn("To sign Kara into GitHub:")
        print_fn(f"  1. Open: {verification_uri}")
        print_fn(f"  2. Enter code: {user_code}")
        print_fn(f"Waiting for browser sign-in... (Ctrl+C to cancel, expires in {expires_in // 60} min)")

        deadline = time.monotonic() + expires_in
        payload: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            poll_resp = client.post(
                ACCESS_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            if poll_resp.status_code != 200:
                raise GitHubAuthError(f"Device-code polling failed with HTTP {poll_resp.status_code}.")
            data = poll_resp.json()
            error = data.get("error")
            if error is None and data.get("access_token"):
                payload = data
                break
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                poll_interval = max(poll_interval + 5, int(data.get("interval") or poll_interval))
                continue
            if error == "expired_token":
                raise GitHubAuthError("GitHub device login expired before it was approved. Try again.")
            if error == "access_denied":
                raise GitHubAuthError("GitHub device login was denied.")
            raise GitHubAuthError(f"GitHub device login failed: {data.get('error_description') or error}")

        if payload is None:
            raise GitHubAuthError("GitHub device login timed out.")
        return payload


def login() -> None:
    payload = device_login()
    save_tokens(payload)
    print()
    print("GitHub login saved for Kara.")
    print(f"Auth state: {auth_file()}")
    print(f"Granted scopes: {payload.get('scope') or _scopes()}")


def status() -> None:
    try:
        state = read_tokens()
    except auth_store.AuthStoreError as exc:
        print(str(exc))
        return
    print("GitHub credentials are stored for Kara.")
    print(f"Auth state: {auth_file()}")
    print(f"Scope: {state.get('scope') or 'unknown'}")
    print(f"Last refresh: {state.get('last_refresh') or 'unknown'}")
    has_refresh = bool(state["tokens"].get("refresh_token"))
    print(f"Refresh token present: {has_refresh} (only set if this OAuth App has expiring tokens enabled)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Kara's GitHub OAuth login")
    parser.add_argument("command", choices=["login", "status", "refresh", "credential"])
    parser.add_argument("operation", nargs="?")
    args = parser.parse_args()
    if args.command == "login":
        login()
    elif args.command == "status":
        status()
    elif args.command == "refresh":
        refresh_tokens()
        print("GitHub token refreshed.")
    elif args.command == "credential":
        return git_credential_helper(args.operation or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
