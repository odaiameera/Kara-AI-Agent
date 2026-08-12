"""Ollama chat provider — delegates HTTP calls to ollama_client.

STUDY GUIDE
-----------
* ``OllamaProvider`` wraps a ``Provider`` config record and implements ``ChatProvider``.
* All network I/O stays in ``ollama_client``; this class is a thin adapter.
* Key concepts: composition, delegation, adapter pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import ollama_client
from provider_base import ChatResult, chat_result_from_ollama
from providers import Provider


@dataclass(frozen=True)
class OllamaProvider:
    """ChatProvider implementation backed by Ollama's REST API."""

    _config: Provider

    @property
    def id(self) -> str:
        return self._config.id

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def type(self) -> str:
        return self._config.type

    @property
    def host(self) -> str:
        return self._config.host

    @property
    def api_key_env(self) -> str | None:
        return self._config.api_key_env

    @property
    def has_credentials(self) -> bool:
        return self._config.has_credentials

    def is_reachable(self) -> bool:
        return ollama_client.is_reachable(self._config)

    def list_models(self) -> list[str]:
        if self.api_key_env and not self.has_credentials:
            raise ollama_client.OllamaError(
                f"No API key configured (set {self.api_key_env} in .env)"
            )
        return ollama_client.list_models(self._config)

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> ChatResult:
        data = ollama_client.chat(
            self._config, model, messages, tools=tools, temperature=temperature
        )
        return chat_result_from_ollama(data)

    def embed(self, text: str, model: str | None = None) -> list[float]:
        return ollama_client.embed(self._config, text, model=model)

    def embed_batch(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        return ollama_client.embed_batch(self._config, texts, model=model)
