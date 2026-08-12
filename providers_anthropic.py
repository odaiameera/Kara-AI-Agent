"""Anthropic Messages API provider.

Anthropic speaks ``/v1/messages``, which is a different wire format from
OpenAI's ``/chat/completions`` — so the generic openai-compatible adapter does
not work here and this one is not optional. Four things differ from every other
adapter Kara has, and each has a helper below:

* ``system`` is a top-level request field, not a message role.
* Tool schemas are flat (``name``/``description``/``input_schema``), not nested
  under ``function``.
* Tool results are ``tool_result`` blocks inside a **user** message, and every
  result from one assistant turn must arrive in a *single* user message.
* Assistant tool calls are ``tool_use`` blocks whose ``input`` is a parsed
  object, not a JSON string.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

import config
from provider_base import (
    ChatResult,
    ProviderError,
    ToolCall,
    Usage,
    is_retryable_status,
    parse_tool_arguments,
)
from providers import Provider

API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"

# Models Kara offers when the account's list cannot be fetched. Anthropic has no
# public list-models endpoint on this API surface, so this is the whole menu.
KNOWN_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-fable-5",
)


def _system_prompt_and_rest(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Split Kara's history into the top-level system prompt and the rest.

    Kara's first message is a system message; Anthropic wants that content in a
    dedicated request field. The trailing ephemeral clock is also system-role but
    is *not* the system prompt — mid-conversation system messages are model-gated
    here, so it is re-tagged as a user message and left at the end, which is also
    what keeps the cacheable prefix stable.
    """
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "system":
            rest.append(message)
        elif message.get("ephemeral"):
            rest.append({"role": "user", "content": message.get("content") or ""})
        else:
            text = str(message.get("content") or "").strip()
            if text:
                system_parts.append(text)
    return "\n\n".join(system_parts), rest


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Content blocks for one assistant turn.

    Provider-native blocks are replayed verbatim when present: Anthropic's
    thinking blocks must come back unchanged, and rebuilding them from the
    flattened text would not round-trip.
    """
    preserved = message.get("provider_content")
    if preserved:
        return list(preserved)

    blocks: list[dict[str, Any]] = []
    content = str(message.get("content") or "")
    if content:
        blocks.append({"type": "text", "text": content})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": name,
                # Kara stores arguments as a JSON string; Anthropic wants an object.
                "input": parse_tool_arguments(function.get("arguments")),
            }
        )
    return blocks


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Kara's history into Anthropic ``messages``.

    Consecutive ``tool`` messages are coalesced into one user turn. Splitting
    them across separate messages is accepted but discourages the model from
    making parallel tool calls at all — which matters here because Kara runs
    read-only tool batches concurrently and gets several results at once.
    """
    converted: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            converted.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        role = message.get("role")

        if role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": str(message.get("content") or ""),
            }
            if message.get("is_error"):
                block["is_error"] = True
            pending_results.append(block)
            continue

        flush_results()

        if role == "assistant":
            blocks = _assistant_blocks(message)
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
        else:
            converted.append(
                {"role": "user", "content": str(message.get("content") or "")}
            )

    flush_results()
    return converted


def to_anthropic_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Flatten Kara's OpenAI-shaped tool schemas into Anthropic's shape."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        converted.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return converted


def chat_result_from_anthropic(data: Any) -> ChatResult:
    """Map an Anthropic ``/v1/messages`` response onto ChatResult."""
    if not isinstance(data, dict):
        raise ProviderError(
            f"Anthropic returned {type(data).__name__}, expected an object."
        )
    blocks = data.get("content")
    if not isinstance(blocks, list):
        error = data.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        raise ProviderError(
            "Anthropic response contained no content list"
            + (f": {detail}" if detail else f"; keys were {sorted(data)[:8]}.")
        )

    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_parts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            calls.append(
                ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=block.get("input")
                    if isinstance(block.get("input"), dict)
                    else {},
                )
            )

    stop_reason = str(data.get("stop_reason") or "stop")
    content = "".join(text_parts)
    if stop_reason == "refusal" and not content:
        # A refusal returns HTTP 200 with possibly-empty content. Say so rather
        # than letting the caller report a generic "no response".
        content = (
            "The model declined to answer this request. Rephrasing it, or "
            "switching provider with /provider, may help."
        )

    usage_raw = data.get("usage")
    usage = (
        Usage(
            prompt_tokens=int(usage_raw.get("input_tokens") or 0),
            completion_tokens=int(usage_raw.get("output_tokens") or 0),
        )
        if isinstance(usage_raw, dict)
        else Usage()
    )

    return ChatResult(
        content=content,
        tool_calls=tuple(calls),
        usage=usage,
        finish_reason="tool_calls" if calls else stop_reason,
        role=str(data.get("role") or "assistant"),
        raw=data,
        # Kept so the next turn replays thinking blocks byte-identically.
        provider_content=[b for b in blocks if isinstance(b, dict)],
    )


@dataclass(frozen=True)
class AnthropicProvider:
    """ChatProvider implementation for the Anthropic Messages API."""

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
        return bool(self._config.api_key)

    def _headers(self) -> dict[str, str]:
        # Anthropic authenticates with x-api-key, not an Authorization bearer.
        return {
            "content-type": "application/json",
            "anthropic-version": API_VERSION,
            "x-api-key": self._config.api_key or "",
        }

    def is_reachable(self) -> bool:
        return self.has_credentials

    def list_models(self) -> list[str]:
        if not self.has_credentials:
            raise ProviderError(
                f"No API key configured (set {self.api_key_env} in .env)"
            )
        return list(KNOWN_MODELS)

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> ChatResult:
        del temperature  # Current Claude models reject sampling parameters.
        system_prompt, rest = _system_prompt_and_rest(messages)

        payload: dict[str, Any] = {
            "model": model,
            # Required by this API, and it bounds thinking plus response text
            # together — not just the visible answer.
            "max_tokens": config.MAX_OUTPUT_TOKENS,
            "messages": to_anthropic_messages(rest),
        }
        if system_prompt:
            # cache_control on the system block: the prompt and tool schemas are
            # the stable prefix, which is why the runtime clock was moved out of
            # the system message and onto the end of the conversation.
            payload["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        converted_tools = to_anthropic_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools

        try:
            resp = httpx.post(
                f"{self.host}/v1/messages",
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
                f"Anthropic chat HTTP {status}: {detail}",
                retryable=is_retryable_status(status),
                status_code=status,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                f"Could not reach Anthropic at {self.host}: {exc}", retryable=True
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Anthropic returned invalid JSON: {exc}") from exc

        return chat_result_from_anthropic(data)

    def embed(self, text: str, model: str | None = None) -> list[float]:
        del text, model
        raise ProviderError(
            "Anthropic does not provide an embeddings endpoint; Kara's memory "
            "search continues to use Ollama embeddings."
        )

    def embed_batch(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        del texts, model
        raise ProviderError(
            "Anthropic does not provide an embeddings endpoint; Kara's memory "
            "search continues to use Ollama embeddings."
        )
