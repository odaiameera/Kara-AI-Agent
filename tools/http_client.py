"""Shared HTTP helpers for web tools (Cloudflare Access, timeouts).
"""
from __future__ import annotations

import os

import httpx

import config

DEFAULT_TIMEOUT = float(os.getenv("WEB_HTTP_TIMEOUT", "30"))
USER_AGENT = os.getenv(
    "WEB_USER_AGENT",
    "KaraBot/1.0 (+https://github.com/odaiameera/Kara-AI-Agent)",
)

def cloudflare_access_headers() -> dict[str, str]:
    """Headers for Cloudflare Access-protected services (e.g. SearXNG)."""
    headers: dict[str, str] = {}
    client_id = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
    client_secret = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    cookie = os.getenv("CF_ACCESS_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers

def default_headers(*, accept: str = "*/*") -> dict[str, str]:
    # LEARN: ``**cloudflare_access_headers()`` merges another dict into this one (dict unpacking).
    h = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        **cloudflare_access_headers(),
    }
    return h

# LEARN: One shared client per process — httpx.Client is thread-safe and keeps
# a connection pool, so repeated search/fetch calls reuse open connections.
_shared_client: httpx.Client | None = None

def get_client() -> httpx.Client:
    """Return the shared HTTP client (created lazily on first use).

    Callers must NOT close it (no ``with`` block) — it lives for the process.
    """
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers=default_headers(),
        )
    return _shared_client
