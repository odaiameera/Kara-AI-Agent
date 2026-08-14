"""Generic OpenAI-compatible chat provider.

One adapter for every backend that speaks the OpenAI ``/chat/completions``
shape — OpenAI itself, Groq, Together, OpenRouter, DeepSeek, Mistral, Fireworks,
vLLM, LM Studio, llama.cpp's server, and anything else following that contract.
Adding one needs no code: set a base URL and an API key env var in ``.env`` and
it is discovered as a provider.

Everything downstream already speaks ``ChatResult``, so this file only has to
map one request and one response shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

import config
from providers.base import (
    ChatResult,
    ProviderError,
    Usage,
    is_retryable_status,
    tool_calls_from_openai_shape,
)
from providers.registry import Provider


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    """ChatProvider implementation for any OpenAI-compatible HTTP endpoint."""

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
        # Local servers (vLLM, LM Studio, llama.cpp) usually need no key at all.
        return bool(self._config.api_key) or not self._config.api_key_env

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def is_reachable(self) -> bool:
        try:
            resp = httpx.get(
                f"{self.host}/models", headers=self._headers(), timeout=5.0
            )
            return resp.status_code < 500
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            resp = httpx.get(
                f"{self.host}/models", headers=self._headers(), timeout=15.0
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise ProviderError(
                f"Could not list models from {self.name}: {exc}"
            ) from exc

        entries = payload.get("data", []) if isinstance(payload, dict) else []
        names: list[str] = []
        for item in entries:
            identifier = item.get("id") if isinstance(item, dict) else None
            if isinstance(identifier, str) and identifier and identifier not in names:
                names.append(identifier)
        return names

    @staticmethod
    def _request_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Kara's history into chat-completions messages."""
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")

            if role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "content": content,
                        "tool_call_id": str(message.get("tool_call_id") or ""),
                    }
                )
                continue

            entry: dict[str, Any] = {"role": role, "content": content}
            if role == "assistant" and message.get("tool_calls"):
                entry["tool_calls"] = message["tool_calls"]
                # The API rejects an assistant turn with both empty content and
                # tool calls on some backends; null is the accepted form.
                entry["content"] = content or None
            converted.append(entry)
        return converted

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._request_messages(messages),
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = httpx.post(
                f"{self.host}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=config.PROVIDER_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = exc.response.text[:300] if exc.response.text else str(exc)
            raise ProviderError(
                f"{self.name} chat HTTP {status}: {detail}",
                retryable=is_retryable_status(status),
                status_code=status,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                f"Could not reach {self.name} at {self.host}: {exc}", retryable=True
            ) from exc
        except ValueError as exc:
            raise ProviderError(f"{self.name} returned invalid JSON: {exc}") from exc

        return chat_result_from_openai(data, provider_name=self.name)

    def embed(self, text: str, model: str | None = None) -> list[float]:
        return self.embed_batch([text], model=model)[0]

    def embed_batch(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = httpx.post(
                f"{self.host}/embeddings",
                headers=self._headers(),
                json={"model": model or config.EMBED_MODEL, "input": texts},
                timeout=120.0,
            )
            resp.raise_for_status()
            rows = resp.json().get("data", [])
        except Exception as exc:
            raise ProviderError(f"{self.name} embeddings failed: {exc}") from exc

        vectors = [row.get("embedding") for row in rows if isinstance(row, dict)]
        if len(vectors) != len(texts):
            raise ProviderError(
                f"{self.name} returned {len(vectors)} embeddings for {len(texts)} inputs."
            )
        return vectors


def chat_result_from_openai(data: Any, *, provider_name: str = "provider") -> ChatResult:
    """Map an OpenAI chat-completions response onto ChatResult."""
    if not isinstance(data, dict):
        raise ProviderError(
            f"{provider_name} returned {type(data).__name__}, expected an object."
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        error = data.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        raise ProviderError(
            f"{provider_name} returned no choices"
            + (f": {detail}" if detail else f"; keys were {sorted(data)[:8]}.")
        )

    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderError(f"{provider_name} returned a choice with no message.")

    calls = tool_calls_from_openai_shape(message.get("tool_calls"))
    usage_raw = data.get("usage") or {}
    usage = Usage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
    ) if isinstance(usage_raw, dict) else Usage()

    return ChatResult(
        content=str(message.get("content") or ""),
        tool_calls=calls,
        usage=usage,
        finish_reason="tool_calls" if calls else str(choice.get("finish_reason") or "stop"),
        role=str(message.get("role") or "assistant"),
        raw=data,
    )
