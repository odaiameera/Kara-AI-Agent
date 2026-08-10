"""Shared OAuth token store for Kara's provider logins (``brain/auth.json``).

Both ``codex_auth.py`` and ``github_auth.py`` persist tokens into one file:

    brain/auth.json
      {"version": 1, "providers": {"openai-codex": {...}, "github": {...}}}

They used to carry byte-identical private copies of the load/save helpers, so
any change to the on-disk format (a schema bump, file permissions, locking)
had to be made twice or the two writers would disagree about a file they
jointly own. That layer lives here instead; each auth module keeps only its
own OAuth flow.

STUDY GUIDE
-----------
* One module owns the file format; provider modules own their OAuth flows.
* ``AuthStoreError`` is the shared base — ``CodexAuthError`` and
  ``GitHubAuthError`` subclass it, so ``except AuthStoreError`` catches
  storage failures from either provider.
* Key concepts: single source of truth, exception hierarchies, atomic
  write-then-replace.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

AUTH_FILE_NAME = "auth.json"


class AuthStoreError(RuntimeError):
    """Raised when Kara's shared auth store is unreadable or malformed."""


def auth_file() -> Path:
    """Return Kara's private auth store path under the gitignored brain directory."""
    return config.BRAIN_DIR / AUTH_FILE_NAME


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 ``...Z`` string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_store() -> dict[str, Any]:
    """Read the whole auth store, returning an empty one if it doesn't exist yet."""
    path = auth_file()
    if not path.exists():
        return {"version": 1, "providers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuthStoreError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthStoreError(f"Invalid auth store shape in {path}.")
    data.setdefault("version", 1)
    data.setdefault("providers", {})
    return data


def save_store(data: dict[str, Any]) -> None:
    """Write the whole auth store atomically (write to .tmp, then replace)."""
    config.ensure_brain()
    path = auth_file()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def read_provider(provider_id: str) -> dict[str, Any] | None:
    """Return one provider's stored entry, or None when it isn't logged in."""
    state = load_store().get("providers", {}).get(provider_id)
    return state if isinstance(state, dict) else None


def write_provider(provider_id: str, entry: dict[str, Any]) -> None:
    """Merge one provider's entry into the store, leaving other providers intact."""
    store = load_store()
    store.setdefault("providers", {})[provider_id] = entry
    save_store(store)
