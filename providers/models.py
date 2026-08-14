"""Active model/provider selection and ``/models`` formatting.

STUDY GUIDE
-----------
* Reads/writes ``brain/settings.json`` for the user's chosen provider and model.
* Formats human-readable lists for /models and /model slash commands.
* Key concepts: JSON persistence, fallback defaults, string building with list joins.
"""
from __future__ import annotations

import json

import config
from providers import registry as providers
from providers.base import ChatProvider

SETTINGS_FILE = config.BRAIN_DIR / "settings.json"
DEFAULT_MODEL = config.OLLAMA_MODEL
DEFAULT_PROVIDER_ID = "ollama-cloud"
PROVIDER_DEFAULT_MODELS = {
    "openai-codex": "gpt-5.5",
    # gpt-oss:20b is confirmed to work on the current Ollama Cloud key; glm-5.2 returns subscription 403.
    "ollama-cloud": "gpt-oss:20b",
    "ollama": DEFAULT_MODEL,
}


def _load_settings() -> dict:
    # LEARN: try/except on read — corrupt JSON falls back to empty dict instead of crashing.
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_settings(data: dict) -> None:
    config.ensure_brain()
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_active_provider_id() -> str:
    saved = _load_settings().get("provider_id", "").strip()
    if saved and providers.get_provider(saved):
        return saved
    # LEARN: Loop until we find a provider that actually has an API key configured.
    for p in providers.load_providers():
        if p.has_credentials:
            return p.id
    return DEFAULT_PROVIDER_ID


def get_current_model() -> str:
    return _load_settings().get("model", "").strip() or DEFAULT_MODEL


def get_active_provider() -> ChatProvider:
    provider = providers.get_chat_provider(get_active_provider_id())
    if provider is None:
        all_p = providers.load_chat_providers()
        if not all_p:
            raise RuntimeError("No providers configured. Check brain/providers.json and .env")
        provider = all_p[0]
    return provider


def set_active(provider_id: str, model: str) -> None:
    provider_id = provider_id.strip()
    model = model.strip()
    if not provider_id or not model:
        raise ValueError("provider_id and model are required.")
    if providers.get_provider(provider_id) is None:
        raise ValueError(f"Unknown provider '{provider_id}'.")
    data = _load_settings()
    data["provider_id"] = provider_id
    data["model"] = model
    _save_settings(data)


def default_model_for_provider(provider_id: str) -> str:
    """Return Kara's safe default model for a provider id/type."""
    record = providers.get_provider(provider_id)
    if record is None:
        raise ValueError(f"Unknown provider '{provider_id}'.")
    # A generically-configured backend names its own default in .env; Kara has no
    # built-in knowledge of what models an arbitrary endpoint serves.
    return (
        record.default_model
        or PROVIDER_DEFAULT_MODELS.get(provider_id)
        or PROVIDER_DEFAULT_MODELS.get(record.type)
        or DEFAULT_MODEL
    )


def select_provider(provider_id: str, model: str | None = None) -> tuple[str, str]:
    """Persist a provider switch and return ``(provider_id, model)``."""
    provider_id = provider_id.strip()
    chosen_model = (model or "").strip() or default_model_for_provider(provider_id)
    set_active(provider_id, chosen_model)
    return provider_id, chosen_model


def parse_model_target(target: str, *, current_provider_id: str) -> tuple[str, str]:
    """Parse ``/model`` targets.

    Supported forms:
    * ``model-name`` keeps the active provider.
    * ``provider-id/model-name`` switches provider and model together.
    """
    target = target.strip()
    if not target:
        raise ValueError("Model name is required.")
    if "/" in target:
        provider_id, model = target.split("/", 1)
        provider_id = provider_id.strip()
        model = model.strip()
        if not provider_id or not model:
            raise ValueError("Use /model provider-id/model-name")
        if providers.get_provider(provider_id) is None:
            raise ValueError(f"Unknown provider '{provider_id}'. Use /providers to list providers.")
        return provider_id, model
    if providers.get_provider(current_provider_id) is None:
        raise ValueError(f"Unknown active provider '{current_provider_id}'.")
    return current_provider_id, target


def format_providers_list() -> str:
    """Format output for native provider switching commands."""
    active_provider_id = get_active_provider_id()
    active_model = get_current_model()
    records = providers.load_providers()
    if not records:
        return "No providers configured."
    lines = ["Providers:\n"]
    for p in records:
        active = " *" if p.id == active_provider_id else ""
        try:
            runtime_provider = providers.to_chat_provider(p)
            ready = runtime_provider.has_credentials
        except Exception:
            ready = p.has_credentials
        status = "ready" if ready else "login/API key needed"
        lines.append(f"- {p.id}{active}")
        lines.append(f"  name: {p.name}")
        lines.append(f"  type: {p.type}")
        lines.append(f"  status: {status}")
        lines.append(f"  switch: /provider {p.id}")
        lines.append(f"  model switch: /model {p.id}/{default_model_for_provider(p.id)}")
    lines.append(f"\nActive: {active_provider_id} / {active_model}")
    return "\n".join(lines)


def format_models_list() -> str:
    """Format output for the ``/models`` command — all providers + their models."""
    active_provider_id = get_active_provider_id()
    active_model = get_current_model()
    discovered = providers.discover_all_models()

    if not discovered:
        return "No providers configured.\nAdd API keys to .env (OLLAMA_API_KEY, OLLAMA_API_KEY_<NAME>)."

    # LEARN: Build output line-by-line, then "\n".join(lines) merges into one string.
    lines = ["Providers and available models:\n"]
    for entry in discovered:
        p = entry.provider
        lines.append(f"[{p.name}] ({p.id})")
        lines.append(f"  host: {p.host}")
        if entry.error:
            lines.append(f"  status: {entry.error}")
        elif not entry.models:
            lines.append("  status: no models returned")
        else:
            shown = entry.models[:25]
            for name in shown:
                active = p.id == active_provider_id and name == active_model
                lines.append(f"  - {name}{' *' if active else ''}")
            if len(entry.models) > 25:
                lines.append(f"  ... and {len(entry.models) - 25} more")
        lines.append("")

    lines.append(f"Active: {active_provider_id} / {active_model}")
    lines.append("\nSwitch provider: /provider <provider-id>")
    lines.append("Switch provider+model: /model <provider-id>/<model-name>")
    return "\n".join(lines)


def format_model_list() -> str:
    """Models for the *active* provider only (legacy ``/model`` without args)."""
    active = get_active_provider()
    active_model = get_current_model()
    lines = [f"Models for active provider [{active.name}] ({active.id}):\n"]

    try:
        names = active.list_models()
    except Exception as e:
        return f"Could not fetch models for {active.name}: {e}\n\nUse /models to see all providers."

    for name in names[:30]:
        lines.append(f"  - {name}{' *' if name == active_model else ''}")
    if len(names) > 30:
        lines.append(f"  ... and {len(names) - 30} more")
    lines.append(f"\nActive: {active_model}")
    lines.append("Use /models to see all providers.")
    return "\n".join(lines)
