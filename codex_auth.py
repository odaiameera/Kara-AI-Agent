"""OpenAI Codex OAuth for Kara (ChatGPT-backed device-code flow).

STUDY GUIDE
-----------
* Runs the OpenAI Codex device-code login flow and stores tokens in ``brain/auth.json``.
* Refreshes access tokens with the saved refresh token before provider calls.
* Key concepts: OAuth device flow, JSON token storage, JWT expiry checks, small CLI entry points.
"""
from __future__ import annotations

import argparse
import base64
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

import auth_store
from auth_store import auth_file  # re-exported: callers use codex_auth.auth_file()

CODEX_PROVIDER_ID = "openai-codex"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_DEVICE_USERCODE_URL = f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = f"{CODEX_ISSUER}/api/accounts/deviceauth/token"
CODEX_VERIFY_URL = f"{CODEX_ISSUER}/codex/device"
CODEX_REDIRECT_URI = f"{CODEX_ISSUER}/deviceauth/callback"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
REFRESH_SKEW_SECONDS = 120


class CodexAuthError(auth_store.AuthStoreError):
    """Raised when OpenAI Codex OAuth state is missing or invalid."""


def save_tokens(tokens: dict[str, str], *, base_url: str = DEFAULT_CODEX_BASE_URL) -> None:
    """Persist OpenAI Codex OAuth tokens to ``brain/auth.json``.

    Tokens are intentionally not stored in ``.env`` and should never be printed.
    """
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not access_token:
        raise CodexAuthError("OpenAI Codex token response did not include access_token.")
    if not refresh_token:
        raise CodexAuthError("OpenAI Codex token response did not include refresh_token.")
    auth_store.write_provider(
        CODEX_PROVIDER_ID,
        {
            "auth_mode": "chatgpt",
            "base_url": base_url.rstrip("/"),
            "last_refresh": auth_store.now_iso(),
            "tokens": {
                "access_token": access_token,
                "refresh_token": refresh_token,
            },
        },
    )


def read_tokens() -> dict[str, Any]:
    """Read stored OpenAI Codex tokens or raise a clear re-login error."""
    state = auth_store.read_provider(CODEX_PROVIDER_ID)
    if state is None:
        raise CodexAuthError(
            "No OpenAI Codex credentials stored. Run `uv run python codex_auth.py login`."
        )
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexAuthError("OpenAI Codex auth state is missing tokens; login again.")
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not access_token or not refresh_token:
        raise CodexAuthError("OpenAI Codex auth state is incomplete; login again.")
    return state


def _jwt_exp_epoch(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return int(exp) if isinstance(exp, (int, float)) else None


def access_token_is_expiring(token: str, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    """Return True when a JWT access token is expired or near expiry.

    Non-JWT tokens are treated as usable because the backend will reject them if invalid.
    """
    exp = _jwt_exp_epoch(token)
    if exp is None:
        return False
    return exp <= int(time.time()) + int(skew_seconds)


def refresh_tokens() -> dict[str, str]:
    """Refresh and persist the OpenAI Codex access token."""
    state = read_tokens()
    tokens = state["tokens"]
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    with httpx.Client(timeout=httpx.Timeout(20.0), headers={"Accept": "application/json"}) as client:
        response = client.post(
            CODEX_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_CLIENT_ID,
            },
        )
    if response.status_code != 200:
        raise CodexAuthError(
            f"OpenAI Codex token refresh failed with HTTP {response.status_code}; login again if it persists."
        )
    payload = response.json()
    updated = {
        "access_token": str(payload.get("access_token", "") or "").strip(),
        "refresh_token": str(payload.get("refresh_token", refresh_token) or refresh_token).strip(),
    }
    save_tokens(updated, base_url=str(state.get("base_url") or DEFAULT_CODEX_BASE_URL))
    return updated


def runtime_credentials(*, refresh_if_expiring: bool = True) -> dict[str, str]:
    """Return a fresh bearer token + Codex backend URL for provider calls."""
    state = read_tokens()
    tokens = dict(state["tokens"])
    access_token = str(tokens.get("access_token", "") or "").strip()
    if refresh_if_expiring and access_token_is_expiring(access_token):
        tokens = refresh_tokens()
        access_token = tokens["access_token"]
    return {
        "access_token": access_token,
        "base_url": str(state.get("base_url") or DEFAULT_CODEX_BASE_URL).rstrip("/"),
    }


def has_credentials() -> bool:
    # LEARN: catch the shared base — a corrupt auth.json raises AuthStoreError
    # from auth_store, which is NOT a CodexAuthError.
    try:
        read_tokens()
        return True
    except auth_store.AuthStoreError:
        return False


def device_login(*, print_fn: Callable[[str], None] = print) -> dict[str, Any]:
    """Run OpenAI's Codex device-code login flow and return token payload.

    This function performs network I/O and waits for the user to complete browser login.
    """
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        usercode_resp = client.post(
            CODEX_DEVICE_USERCODE_URL,
            json={"client_id": CODEX_CLIENT_ID},
            headers={"Content-Type": "application/json"},
        )
        if usercode_resp.status_code != 200:
            raise CodexAuthError(
                f"Device code request failed with HTTP {usercode_resp.status_code}."
            )
        device_data = usercode_resp.json()
        user_code = str(device_data.get("user_code", "") or "").strip()
        device_auth_id = str(device_data.get("device_auth_id", "") or "").strip()
        poll_interval = max(1, int(device_data.get("interval") or 5))
        if not user_code or not device_auth_id:
            raise CodexAuthError("Device code response was missing user_code/device_auth_id.")

        print_fn("To sign Kara into OpenAI Codex:")
        print_fn(f"  1. Open: {CODEX_VERIFY_URL}")
        print_fn(f"  2. Enter code: {user_code}")
        print_fn("Waiting for browser sign-in... (Ctrl+C to cancel)")

        code_data: dict[str, Any] | None = None
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            poll_resp = client.post(
                CODEX_DEVICE_TOKEN_URL,
                json={"device_auth_id": device_auth_id, "user_code": user_code},
                headers={"Content-Type": "application/json"},
            )
            if poll_resp.status_code == 200:
                code_data = poll_resp.json()
                break
            if poll_resp.status_code in {403, 404}:
                continue
            raise CodexAuthError(
                f"Device-code polling failed with HTTP {poll_resp.status_code}."
            )

        if code_data is None:
            raise CodexAuthError("OpenAI Codex login timed out after 15 minutes.")

        authorization_code = str(code_data.get("authorization_code", "") or "").strip()
        code_verifier = str(code_data.get("code_verifier", "") or "").strip()
        if not authorization_code or not code_verifier:
            raise CodexAuthError("Device auth response was missing authorization_code/code_verifier.")

        token_resp = client.post(
            CODEX_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": CODEX_REDIRECT_URI,
                "client_id": CODEX_CLIENT_ID,
                "code_verifier": code_verifier,
            },
        )
        if token_resp.status_code != 200:
            raise CodexAuthError(
                f"Token exchange failed with HTTP {token_resp.status_code}."
            )
        tokens = token_resp.json()
        return {
            "tokens": {
                "access_token": str(tokens.get("access_token", "") or "").strip(),
                "refresh_token": str(tokens.get("refresh_token", "") or "").strip(),
            },
            "base_url": DEFAULT_CODEX_BASE_URL,
            "last_refresh": auth_store.now_iso(),
            "auth_mode": "chatgpt",
        }


def login() -> None:
    creds = device_login()
    save_tokens(creds["tokens"], base_url=creds.get("base_url", DEFAULT_CODEX_BASE_URL))
    print()
    print("OpenAI Codex login saved for Kara.")
    print(f"Auth state: {auth_file()}")
    print("Next: use `/models` to see OpenAI Codex, or `/provider openai-codex gpt-5.6-terra` to use it.")


def status() -> None:
    try:
        state = read_tokens()
    except auth_store.AuthStoreError as exc:
        print(str(exc))
        return
    token = str(state["tokens"].get("access_token", "") or "")
    exp = _jwt_exp_epoch(token)
    if exp:
        exp_text = datetime.fromtimestamp(exp, timezone.utc).isoformat()
    else:
        exp_text = "unknown"
    print("OpenAI Codex credentials are stored for Kara.")
    print(f"Auth state: {auth_file()}")
    print(f"Access token expiry: {exp_text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Kara's OpenAI Codex OAuth login")
    parser.add_argument("command", choices=["login", "status", "refresh"])
    args = parser.parse_args()
    if args.command == "login":
        login()
    elif args.command == "status":
        status()
    elif args.command == "refresh":
        refresh_tokens()
        print("OpenAI Codex token refreshed.")


if __name__ == "__main__":
    main()
