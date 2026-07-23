"""LLM provider registry: multiple API keys, model discovery, active selection.

Providers are defined in ``brain/providers.json`` (auto-seeded from ``.env`` on
first run). API keys always live in ``.env`` — never in the JSON file.

Env convention for multiple Ollama keys:
  OLLAMA_API_KEY=...           -> provider ``ollama-cloud``
  OLLAMA_API_KEY_WORK=...      -> provider ``ollama-work``
  OLLAMA_API_KEY_<NAME>=...    -> provider ``ollama-<name>`` (lower-case)

A local provider (``ollama-local``) is always included.

Use ``load_chat_providers()`` / ``get_chat_provider()`` for runtime ``ChatProvider``
instances; ``Provider`` records are the persisted configuration layer.

STUDY GUIDE
-----------
* Discovers Ollama providers from environment variables and stores defs in JSON.
* ``Provider`` dataclass holds host, API key, and credential checks.
* ``to_chat_provider()`` builds a ``ChatProvider`` adapter from a config record.
* Key concepts: ``@dataclass``, ``@property``, ``os.environ.items()``, factory pattern.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import config
from provider_base import ChatProvider

PROVIDERS_FILE = config.BRAIN_DIR / "providers.json"


# LEARN: @dataclass(frozen=True) auto-generates __init__/__repr__; frozen makes instances immutable.
@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    type: str  # "ollama" for now; extensible later
    host: str
    api_key: str | None = None
    api_key_env: str | None = None

    # LEARN: @property defines a method accessed like an attribute (no parentheses).
    @property
    def has_credentials(self) -> bool:
        if self.id == "ollama-local":
            return True
        return bool(self.api_key)


def _discover_provider_defs_from_env() -> list[dict]:
    """Build provider definitions by scanning environment variables."""
    defs: list[dict] = []
    seen_ids: set[str] = set()

    primary_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if primary_key:
        host = os.getenv("OLLAMA_HOST", "https://ollama.com").rstrip("/")
        defs.append(
            {
                "id": "ollama-cloud",
                "name": "Ollama Cloud",
                "type": "ollama",
                "host": host,
                "api_key_env": "OLLAMA_API_KEY",
            }
        )
        seen_ids.add("ollama-cloud")

    # LEARN: os.environ.items() loops all env vars — we filter by prefix to find extra API keys.
    for env_name, value in os.environ.items():
        if not env_name.startswith("OLLAMA_API_KEY_") or not value.strip():
            continue
        suffix = env_name[len("OLLAMA_API_KEY_") :].lower().replace("_", "-")
        provider_id = f"ollama-{suffix}"
        if provider_id in seen_ids:
            continue
        defs.append(
            {
                "id": provider_id,
                "name": f"Ollama ({suffix.replace('-', ' ').title()})",
                "type": "ollama",
                "host": "https://ollama.com",
                "api_key_env": env_name,
            }
        )
        seen_ids.add(provider_id)

    local_host = os.getenv("OLLAMA_LOCAL_HOST", "http://localhost:11434").rstrip("/")
    if "ollama-local" not in seen_ids:
        defs.append(
            {
                "id": "ollama-local",
                "name": "Ollama Local",
                "type": "ollama",
                "host": local_host,
                "api_key_env": None,
            }
        )
        seen_ids.add("ollama-local")

    # OpenAI Codex is OAuth-backed, so it does not have an API key env var.
    # Credentials live in brain/auth.json after `uv run python codex_auth.py login`.
    if "openai-codex" not in seen_ids:
        defs.append(
            {
                "id": "openai-codex",
                "name": "OpenAI Codex",
                "type": "openai-codex",
                "host": "https://chatgpt.com/backend-api/codex",
                "api_key_env": None,
            }
        )

    return defs


def seed_providers_file() -> None:
    """Create brain/providers.json from .env on first run."""
    config.ensure_brain()
    defs = _discover_provider_defs_from_env()
    data = {"providers": defs}
    PROVIDERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _resolve_api_key(api_key_env: str | None) -> str | None:
    if not api_key_env:
        return None
    value = os.getenv(api_key_env, "").strip()
    return value or None


def load_providers() -> list[Provider]:
    """Load all configured providers, resolving API keys from .env."""
    if not PROVIDERS_FILE.exists():
        seed_providers_file()
    else:
        _sync_providers_from_env()

    try:
        raw = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        seed_providers_file()
        raw = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))

    # LEARN: List comprehension builds Provider objects from JSON dicts.
    providers: list[Provider] = []
    for item in raw.get("providers", []):
        api_key_env = item.get("api_key_env")
        providers.append(
            Provider(
                id=item["id"],
                name=item.get("name", item["id"]),
                type=item.get("type", "ollama"),
                host=item["host"].rstrip("/"),
                api_key=_resolve_api_key(api_key_env),
                api_key_env=api_key_env,
            )
        )
    return providers


def _sync_providers_from_env() -> None:
    """Add any newly discovered env-based providers to providers.json."""
    try:
        raw = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        seed_providers_file()
        return

    existing_ids = {p["id"] for p in raw.get("providers", [])}
    added = False
    for item in _discover_provider_defs_from_env():
        if item["id"] not in existing_ids:
            raw.setdefault("providers", []).append(item)
            existing_ids.add(item["id"])
            added = True

    if added:
        PROVIDERS_FILE.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def get_provider(provider_id: str) -> Provider | None:
    for p in load_providers():
        if p.id == provider_id:
            return p
    return None


def to_chat_provider(provider: Provider) -> ChatProvider:
    """Build a runtime ChatProvider adapter from a persisted Provider record."""
    if provider.type == "ollama":
        from providers_ollama import OllamaProvider

        return OllamaProvider(provider)
    if provider.type == "openai-codex":
        from providers_codex import OpenAICodexProvider

        return OpenAICodexProvider(provider)
    # Future: providers_gemini.GeminiProvider(provider)
    # Future: providers_openai.OpenAIProvider(provider)
    raise RuntimeError(f"Unsupported provider type: {provider.type}")


def get_chat_provider(provider_id: str) -> ChatProvider | None:
    record = get_provider(provider_id)
    return to_chat_provider(record) if record else None


def load_chat_providers() -> list[ChatProvider]:
    return [to_chat_provider(p) for p in load_providers()]


def first_reachable_provider() -> ChatProvider | None:
    """Return the first ChatProvider that responds to a health check."""
    for provider in load_chat_providers():
        if provider.api_key_env and not provider.has_credentials:
            continue
        if provider.is_reachable():
            return provider
    return None


@dataclass
class ProviderModels:
    provider: ChatProvider
    models: list[str]
    error: str | None = None


def discover_all_models() -> list[ProviderModels]:
    """Query every configured provider for its available models."""
    results: list[ProviderModels] = []
    for provider in load_chat_providers():
        if provider.api_key_env and not provider.has_credentials:
            results.append(
                ProviderModels(
                    provider=provider,
                    models=[],
                    error=f"Missing API key ({provider.api_key_env} not set in .env)",
                )
            )
            continue
        try:
            names = provider.list_models()
            results.append(ProviderModels(provider=provider, models=names))
        except Exception as e:
            results.append(
                ProviderModels(provider=provider, models=[], error=str(e))
            )
    return results
