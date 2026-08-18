"""Shared Ollama HTTP client for chat, embeddings, and model listing.

Each call takes an explicit ``Provider`` so Kara can talk to multiple Ollama
hosts/keys (cloud accounts, local daemon, etc.).

"""
from __future__ import annotations

from typing import Any

import httpx

import config
from providers.base import ProviderError, is_retryable_status
from providers.registry import Provider

# LEARN: Subclassing ProviderError gives a named exception type callers can catch specifically.
class OllamaError(ProviderError):
    """Raised when an Ollama API request fails."""
def _headers(provider: Provider) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if provider.api_key:
        h["Authorization"] = f"Bearer {provider.api_key}"
    return h

def is_reachable(provider: Provider) -> bool:
    # LEARN: Bare except returns False on any network/HTTP failure — quick health check.
    try:
        resp = httpx.get(
            f"{provider.host}/api/tags",
            headers=_headers(provider),
            timeout=5.0,
        )
        return resp.status_code == 200
    except Exception:
        return False

def list_models(provider: Provider) -> list[str]:
    """Return available model names from an Ollama provider."""
    try:
        resp = httpx.get(
            f"{provider.host}/api/tags",
            headers=_headers(provider),
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        # LEARN: List comprehension with filter — keep only entries that have a "name" key.
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        raise OllamaError(f"Could not list models from {provider.name}: {e}") from e

def chat(
    provider: Provider,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Call Ollama ``/api/chat`` (non-streaming). Returns the full response object."""
    # num_ctx must be explicit: Ollama defaults to 4096 tokens and silently drops
    # whatever does not fit rather than failing, which would quietly discard the
    # head of the prompt — the system prompt and its safety rules.
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": config.MODEL_CONTEXT_TOKENS,
        },
    }
    if tools:
        payload["tools"] = tools

    try:
        resp = httpx.post(
            f"{provider.host}/api/chat",
            headers=_headers(provider),
            json=payload,
            timeout=300.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:300] if e.response.text else str(e)
        status = e.response.status_code
        raise OllamaError(
            f"Ollama chat HTTP {status} ({provider.name}): {detail}",
            retryable=is_retryable_status(status),
            status_code=status,
        ) from e
    except httpx.RequestError as e:
        # Connection resets and timeouts are transient by nature.
        raise OllamaError(
            f"Could not reach {provider.name} at {provider.host}: {e}",
            retryable=True,
        ) from e

def embed(provider: Provider, text: str, model: str | None = None) -> list[float]:
    """Call Ollama ``/api/embeddings`` for a single string."""
    model = model or config.EMBED_MODEL
    try:
        resp = httpx.post(
            f"{provider.host}/api/embeddings",
            headers=_headers(provider),
            json={"model": model, "prompt": text},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        vec = data.get("embedding")
        if not vec:
            raise OllamaError(f"No embedding returned for model '{model}'.")
        return vec
    except httpx.HTTPStatusError as e:
        raise OllamaError(f"Ollama embeddings HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        raise OllamaError(f"Could not reach {provider.host}: {e}") from e

def embed_batch(
    provider: Provider, texts: list[str], model: str | None = None
) -> list[list[float]]:
    """Embed many texts in one HTTP request via ``/api/embed`` (batch endpoint).

    Falls back to one-request-per-text if the host doesn't support batching.
    """
    if not texts:
        return []
    model = model or config.EMBED_MODEL
    try:
        # LEARN: Newer Ollama exposes /api/embed which accepts "input" as a list —
        # one round-trip instead of N. The older /api/embeddings is single-text only.
        resp = httpx.post(
            f"{provider.host}/api/embed",
            headers=_headers(provider),
            json={"model": model, "input": texts},
            timeout=120.0,
        )
        resp.raise_for_status()
        vecs = resp.json().get("embeddings")
        if isinstance(vecs, list) and len(vecs) == len(texts):
            return vecs
    except Exception:
        pass  # Old server or batch failure — fall back to sequential below.
    return [embed(provider, t, model=model) for t in texts]

# Tool-argument normalization now lives in provider_base.parse_tool_arguments,
# shared by every adapter rather than duplicated per transport.
