"""Chat provider interface for Kara's multi-provider foundation.

STUDY GUIDE
-----------
* Defines the ``ChatProvider`` protocol every LLM backend must implement.
* ``ProviderError`` is the shared base for provider-specific failures.
* Key concepts: ``typing.Protocol``, structural subtyping, extension points.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Raised when a chat provider API request fails."""


@runtime_checkable
class ChatProvider(Protocol):
    """Minimal interface for chat + embedding backends."""

    id: str
    name: str
    type: str
    host: str
    api_key_env: str | None

    @property
    def has_credentials(self) -> bool: ...

    def is_reachable(self) -> bool: ...

    def list_models(self) -> list[str]: ...

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]: ...

    def embed(self, text: str, model: str | None = None) -> list[float]: ...

    def embed_batch(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]: ...


# Future provider modules (not implemented yet):
#
# providers_gemini.py — Google Gemini API (API key in .env)
# providers_openai.py — OpenAI API (API key in .env)
# providers_codex.py — OpenAI Codex via ChatGPT OAuth:
#   provider id: openai-codex
#   auth: ChatGPT OAuth / device code flow
#   token store: brain/auth.json
#   backend: https://chatgpt.com/backend-api/codex
#   Needs Responses API adapter and tool-call normalization
