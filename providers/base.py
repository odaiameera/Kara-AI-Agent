"""Chat provider interface for Kara's multi-provider foundation.

``ChatResult`` is the harness's own representation of one model turn. Adapters
translate their provider's wire format into it, so nothing above this layer has
to know whether a response came from Ollama, Codex, or an OpenAI-compatible
endpoint. Without it the "provider abstraction" is just Ollama's response shape
wearing a Protocol, which is what blocked token accounting and new backends.

"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

import config

_T = TypeVar("_T")

class ProviderError(RuntimeError):
    """Raised when a chat provider API request fails.

    ``retryable`` distinguishes a transient failure (rate limit, gateway error,
    dropped connection) from a real client error. Without it every failure looked
    the same and a single 429 ended the whole turn.
    """
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code

# 429 is rate limiting; 5xx are upstream failures. Both are worth another try.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

def is_retryable_status(status_code: int | None) -> bool:
    return status_code in RETRYABLE_STATUS

def call_with_retry(
    operation: Callable[[], _T],
    *,
    attempts: int | None = None,
    base_delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, ProviderError, float], None] | None = None,
) -> _T:
    """Run ``operation``, retrying transient provider failures with backoff.

    Uses exponential backoff with jitter so several sessions failing at once do
    not retry in lockstep. Non-retryable errors are re-raised immediately.
    """
    total = attempts if attempts is not None else config.PROVIDER_RETRY_ATTEMPTS
    delay = base_delay if base_delay is not None else config.PROVIDER_RETRY_BASE_DELAY

    last: ProviderError | None = None
    for attempt in range(1, total + 1):
        try:
            return operation()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt == total:
                raise
            wait = delay * (2 ** (attempt - 1))
            wait += random.uniform(0, wait * 0.25)  # jitter
            if on_retry:
                on_retry(attempt, exc, wait)
            sleep(wait)
    assert last is not None  # unreachable: the loop either returns or raises
    raise last

def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Normalize tool call arguments (object or JSON string) into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}

@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        """Serialize back into the stored/history format.

        Arguments are always written as a JSON string. Ollama accepts either
        form, but the Codex Responses adapter stringifies whatever it finds — so
        a dict stored verbatim would replay as a Python repr (``{'a': 1}``)
        rather than valid JSON. Normalizing on write keeps replay correct across
        providers.
        """
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }

@dataclass(frozen=True)
class Usage:
    """Token accounting for one request. Zero means the provider did not report."""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def reported(self) -> bool:
        return bool(self.prompt_tokens or self.completion_tokens)

@dataclass(frozen=True)
class ChatResult:
    """One assistant turn, normalized across providers."""
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    role: str = "assistant"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> dict[str, Any]:
        """Build the history/persistence message for this turn.

        The role is always explicit. Persisting a message without one used to
        let a malformed provider payload be stored as a *user* turn, silently
        corrupting the transcript for every later replay.
        """
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.to_wire() for call in self.tool_calls]
        return message

def tool_calls_from_openai_shape(raw_calls: Any) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for index, call in enumerate(raw_calls or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        calls.append(
            ToolCall(
                # Ollama omits ids on some models; synthesize a stable one so
                # tool results can still be correlated on replay.
                id=str(call.get("id") or "") or f"call_{index}",
                name=name,
                arguments=parse_tool_arguments(function.get("arguments")),
            )
        )
    return tuple(calls)

def chat_result_from_ollama(data: Any) -> ChatResult:
    """Map an Ollama ``/api/chat`` response onto ChatResult."""
    if not isinstance(data, dict):
        raise ProviderError(f"Ollama returned {type(data).__name__}, expected an object.")
    message = data.get("message")
    if not isinstance(message, dict):
        raise ProviderError(
            "Ollama response contained no message object; "
            f"keys were {sorted(data)[:8]}."
        )

    # An empty assistant message is structurally valid — the caller reports it as
    # "no response". Only a malformed *shape* is a provider error, because that is
    # what used to be persisted as a phantom user turn.
    tool_calls = tool_calls_from_openai_shape(message.get("tool_calls"))
    finish = str(data.get("done_reason") or "stop")
    return ChatResult(
        content=str(message.get("content") or ""),
        tool_calls=tool_calls,
        usage=Usage(
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
        ),
        finish_reason="tool_calls" if tool_calls else finish,
        role=str(message.get("role") or "assistant"),
        raw=data,
    )

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
    ) -> ChatResult: ...

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
