"""OpenAI Codex Responses provider backed by Kara's OAuth store.

The ChatGPT Codex backend requires Server-Sent Events (``stream=true``), even
when Kara is collecting a complete response before returning it to its normal
agent loop. This adapter translates between Kara's OpenAI-style messages/tools
and that Responses/SSE protocol.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from auth import store as auth_store
from auth import codex as codex_auth
from providers.base import (
    ChatResult,
    ProviderError,
    Usage,
    is_retryable_status,
    tool_calls_from_openai_shape,
)
from providers.registry import Provider


class OpenAICodexProvider:
    """ChatProvider implementation for ChatGPT OAuth via Codex Responses."""

    def __init__(self, config_record: Provider):
        self._config = config_record

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
        return None

    @property
    def has_credentials(self) -> bool:
        return codex_auth.has_credentials()

    def _creds(self) -> dict[str, str]:
        # LEARN: AuthStoreError is the shared base of CodexAuthError, so this
        # also converts a corrupt brain/auth.json into a clean ProviderError.
        try:
            return codex_auth.runtime_credentials()
        except auth_store.AuthStoreError as exc:
            raise ProviderError(str(exc)) from exc

    @staticmethod
    def _headers(access_token: str) -> dict[str, str]:
        """Use Codex CLI-shaped headers required by the ChatGPT backend."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs/0.0.0 (Kara)",
            "originator": "codex_cli_rs",
        }
        try:
            payload = access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                headers["ChatGPT-Account-ID"] = account_id
        except Exception:
            pass
        return headers

    @staticmethod
    def _responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools or []:
            function = tool.get("function", {}) if isinstance(tool, dict) else {}
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            converted.append(
                {
                    "type": "function",
                    "name": name,
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        return converted

    @staticmethod
    def _responses_request(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Split the system prompt into Responses instructions and convert history."""
        instructions = "You are Kara, a helpful personal assistant."
        items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")
            if role == "system" and message.get("ephemeral"):
                # A trailing note (the runtime clock) rather than the real system
                # prompt. It must not overwrite `instructions`, and keeping it out
                # of that field is also what leaves the cacheable prefix stable.
                if content:
                    items.append({"role": "user", "content": content})
            elif role == "system":
                instructions = content or instructions
            elif role == "tool":
                call_id = str(message.get("tool_call_id") or "").strip()
                if call_id:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": content,
                        }
                    )
                else:
                    # Sessions created before tool_call_id persistence still
                    # retain useful context; replay their result as user text.
                    items.append({"role": "user", "content": f"Tool result ({message.get('tool_name') or 'tool'}): {content}"})
            elif role == "assistant" and message.get("tool_calls"):
                if content:
                    items.append({"role": "assistant", "content": content})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    call_id = str(call.get("id") or "").strip()
                    name = str(function.get("name") or "").strip()
                    arguments = str(function.get("arguments") or "{}")
                    if call_id and name:
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": name,
                                "arguments": arguments,
                            }
                        )
            elif role in {"user", "assistant"}:
                items.append({"role": role, "content": content})
        return instructions, items or [{"role": "user", "content": ""}]

    def is_reachable(self) -> bool:
        return self.has_credentials

    def list_models(self) -> list[str]:
        creds = self._creds()
        url = f"{creds['base_url']}/models?client_version=1.0.0"
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {creds['access_token']}", "Accept": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise ProviderError(f"Could not list OpenAI Codex models: {exc}") from exc
        entries = data.get("models", []) if isinstance(data, dict) else []
        models: list[str] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug") or item.get("id") or item.get("name")
            if isinstance(slug, str) and slug.strip() and slug not in models:
                visibility = str(item.get("visibility", "") or "").lower()
                if visibility not in {"hide", "hidden"}:
                    models.append(slug.strip())
        return models or ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"]

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> ChatResult:
        del temperature  # ChatGPT Codex rejects the chat-completions temperature dial.
        creds = self._creds()
        instructions, input_items = self._responses_request(messages)
        response_tools = self._responses_tools(tools)
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if response_tools:
            payload["tools"] = response_tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage = Usage()
        finish_reason = "stop"
        url = f"{creds['base_url']}/responses"
        try:
            with httpx.stream(
                "POST", url, headers=self._headers(creds["access_token"]), json=payload, timeout=180.0
            ) as response:
                if response.status_code >= 400:
                    # httpx streaming responses intentionally forbid `.text`
                    # until the body has been consumed. Read first so the real
                    # API error is shown instead of masking it with ResponseNotRead.
                    response.read()
                    raise ProviderError(
                        f"OpenAI Codex chat HTTP {response.status_code}: {response.text[:500]}",
                        retryable=is_retryable_status(response.status_code),
                        status_code=response.status_code,
                    )
                for raw_line in response.iter_lines():
                    if not raw_line or not raw_line.startswith("data: "):
                        continue
                    raw_data = raw_line[6:]
                    if raw_data == "[DONE]":
                        break
                    event = json.loads(raw_data)
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        text_parts.append(str(event.get("delta") or ""))
                    elif kind == "response.output_item.done":
                        item = event.get("item") or {}
                        if item.get("type") == "function_call":
                            tool_calls.append(
                                {
                                    "id": str(item.get("call_id") or item.get("id") or ""),
                                    "type": "function",
                                    "function": {
                                        "name": str(item.get("name") or ""),
                                        "arguments": str(item.get("arguments") or "{}"),
                                    },
                                }
                            )
                    elif kind == "response.completed":
                        # The terminal event carries token accounting. Kara used
                        # to drop this event entirely, which is why no usage data
                        # existed for the Codex provider.
                        response_body = event.get("response") or {}
                        usage = self._usage_from_response(response_body)
                        finish_reason = str(
                            response_body.get("incomplete_details", {}).get("reason")
                            or "stop"
                        )
                    elif kind in {"response.failed", "error"}:
                        raise ProviderError(f"OpenAI Codex response failed: {event.get('error') or event}")
        except ProviderError:
            raise
        except httpx.RequestError as exc:
            raise ProviderError(
                f"Could not reach OpenAI Codex: {exc}", retryable=True
            ) from exc
        except Exception as exc:
            raise ProviderError(f"OpenAI Codex chat request failed: {exc}") from exc

        calls = tool_calls_from_openai_shape(tool_calls)
        return ChatResult(
            content="".join(text_parts),
            tool_calls=calls,
            usage=usage,
            finish_reason="tool_calls" if calls else finish_reason,
            raw={"tool_calls": tool_calls},
        )

    @staticmethod
    def _usage_from_response(response_body: dict[str, Any]) -> Usage:
        raw = response_body.get("usage") or {}
        if not isinstance(raw, dict):
            return Usage()
        return Usage(
            prompt_tokens=int(raw.get("input_tokens") or 0),
            completion_tokens=int(raw.get("output_tokens") or 0),
        )

    def embed(self, text: str, model: str | None = None) -> list[float]:
        del text, model
        raise ProviderError("OpenAI Codex embeddings are not implemented; Kara memory still uses Ollama embeddings.")

    def embed_batch(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        del texts, model
        raise ProviderError("OpenAI Codex embeddings are not implemented; Kara memory still uses Ollama embeddings.")
