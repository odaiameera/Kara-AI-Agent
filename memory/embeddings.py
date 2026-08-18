"""Local embeddings via the active chat provider (prefers local Ollama).

"""
from __future__ import annotations

import time

from providers import registry as providers
from providers.base import ChatProvider, ProviderError

EmbeddingError = ProviderError

# LEARN: Cache the working provider for a short time — health-checking on every
# embed call adds an HTTP round-trip per search. Failures are not cached, so a
# provider coming back online is picked up immediately.
_PROVIDER_TTL = 60.0
_provider_cache: tuple[float, ChatProvider] | None = None

def _embed_provider() -> ChatProvider | None:
    # LEARN: Leading underscore marks a "private" helper — convention, not enforced by Python.
    global _provider_cache
    now = time.monotonic()
    if _provider_cache and (now - _provider_cache[0]) < _PROVIDER_TTL:
        return _provider_cache[1]
    local = providers.get_chat_provider("ollama-local")
    if local and local.is_reachable():
        found = local
    else:
        found = providers.first_reachable_provider()
    if found is not None:
        _provider_cache = (now, found)
    return found

def is_available() -> bool:
    # LEARN: _embed_provider only returns providers that passed a reachability
    # check, so no second health check is needed here.
    return _embed_provider() is not None

def embed(text: str) -> list[float]:
    # LEARN: Raises EmbeddingError if no provider — callers use try/except or is_available() first.
    provider = _embed_provider()
    if provider is None:
        raise EmbeddingError("No reachable provider for embeddings.")
    return provider.embed(text)

def embed_batch(texts: list[str]) -> list[list[float]]:
    provider = _embed_provider()
    if provider is None:
        raise EmbeddingError("No reachable provider for embeddings.")
    return provider.embed_batch(texts)
