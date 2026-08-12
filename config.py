"""Central configuration and path resolution for the Kara agent.

Importing this module loads environment variables from ``personal_agent/.env``
and exposes the key filesystem locations the rest of the app relies on.

Kara's entire mind lives in a single self-contained, gitignored directory: the
"brain" (``personal_agent/brain/``). It holds:

    brain/
      core/        always-in-context working memory (persona/human/active_task)
      learnings/   durable facts & insights (semantic memory)
      sessions/    episodic logs of past conversations
      index/       derived vector store (embeddings cache)

Everything under ``brain/`` is runtime state and is gitignored. The committed
``memory/IDENTITY.md`` acts only as a seed for Kara's persona on first run.

STUDY GUIDE
-----------
* Loads settings from ``.env`` and defines every path Kara uses (brain, logs, index).
* Resolves optional Obsidian vault and Telegram allow-list from environment variables.
* Creates the brain directory tree on import so the app can start cleanly.
* Key concepts: ``pathlib.Path``, ``os.getenv``, module-level constants, side effects on import.
"""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# LEARN: __file__ is the path to this source file; .resolve().parent gives the folder
# containing config.py (the personal_agent package). Path objects make path joining safe.
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent

# LEARN: load_dotenv reads KEY=value lines from .env into os.environ so os.getenv works.
# We pass an explicit path so it works no matter which directory you run commands from.
load_dotenv(PACKAGE_DIR / ".env")

# LEARN: These are module-level constants — shared paths every other file imports from here.
# Using Path / "subdir" joins path segments in an OS-safe way (forward slashes on all OSes).
BRAIN_DIR = PACKAGE_DIR / "brain"
CORE_DIR = BRAIN_DIR / "core"
LEARNINGS_DIR = BRAIN_DIR / "learnings"
SESSIONS_DIR = BRAIN_DIR / "sessions"
INDEX_DIR = BRAIN_DIR / "index"
INDEX_FILE = INDEX_DIR / "index.json"
LOG_DIR = BRAIN_DIR / "logs"
GATEWAY_LOG = LOG_DIR / "gateway.log"
GATEWAY_PID_FILE = BRAIN_DIR / "gateway.pid"
RESTART_FLAG = BRAIN_DIR / "restart.flag"
RESTART_NOTIFY_FILE = BRAIN_DIR / "restart_notify.json"
RESTART_LOCK_FILE = BRAIN_DIR / "restart.lock"
GATEWAY_INSTANCE_LOCK = BRAIN_DIR / "gateway.instance.lock"
CODE_FINGERPRINT_FILE = BRAIN_DIR / "code.fingerprint"
SESSIONS_MIGRATED_MARKER = BRAIN_DIR / "sessions_migrated.marker"

# LEARN: A dict maps string keys to Path values — handy for named memory sections.
CORE_FILES = {
    "persona": CORE_DIR / "persona.md",
    "human": CORE_DIR / "human.md",
    "active_task": CORE_DIR / "active_task.md",
}

IDENTITY_SEED = REPO_ROOT / "memory" / "IDENTITY.md"

# LEARN: os.getenv("KEY", default) reads env vars; .strip() removes accidental whitespace.
# The ternary ``A if condition else B`` picks cloud vs local Ollama host automatically.
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
_default_host = "https://ollama.com" if OLLAMA_API_KEY else "http://localhost:11434"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", _default_host).rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:120b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")


def _positive_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value >= minimum else default


# Context window Kara asks the model for. Ollama silently truncates a prompt that
# exceeds num_ctx, and its default is 4096 — smaller than Kara's system prompt
# plus baseline tool schemas — so leaving this unset drops the head of the
# conversation, including the safety rules, without any error.
MODEL_CONTEXT_TOKENS = _positive_int_env("KARA_MODEL_CONTEXT_TOKENS", 32768, minimum=2048)

# Bounds on a single turn. The tool loop is otherwise unbounded: a model that
# keeps requesting tools spins forever, burning tokens and holding the worker
# thread with no way to stop it.
MAX_TOOL_ITERATIONS = _positive_int_env("KARA_MAX_TOOL_ITERATIONS", 25)
TURN_TIMEOUT_SECONDS = _positive_int_env("KARA_TURN_TIMEOUT_SECONDS", 600, minimum=30)
# How many consecutive identical tool calls (same name and arguments) count as a
# stuck loop rather than legitimate repetition.
MAX_REPEATED_TOOL_CALLS = _positive_int_env("KARA_MAX_REPEATED_TOOL_CALLS", 3, minimum=2)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

SEARXNG_URL = os.getenv("SEARXNG_URL", "https://search.ameera.dev").rstrip("/")


def _configured_path_roots(name: str, defaults: tuple[Path, ...]) -> tuple[Path, ...]:
    """Parse an os.pathsep-separated allow-list of absolute filesystem roots."""
    raw = os.getenv(name, "").strip()
    candidates = raw.split(os.pathsep) if raw else [str(path) for path in defaults]
    roots: list[Path] = []
    for value in candidates:
        value = value.strip().strip('"').strip("'")
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if path not in roots:
            roots.append(path)
    return tuple(roots)


# General file tools can read/search the project and the user's home directory by
# default. Writes stay inside the project unless the user explicitly expands the
# allow-list in .env. Sensitive credential/profile locations are separately
# blocked by tools/file_tools.py unless KARA_ALLOW_SENSITIVE_FILES=1.
FILE_READ_ROOTS = _configured_path_roots(
    "KARA_FILE_READ_ROOTS", (PACKAGE_DIR, Path.home())
)
FILE_WRITE_ROOTS = _configured_path_roots("KARA_FILE_WRITE_ROOTS", (PACKAGE_DIR,))


# LEARN: @lru_cache(maxsize=1) memoizes the result — .env is loaded once at import,
# so re-parsing the same string on every Telegram message is wasted work.
@lru_cache(maxsize=1)
def telegram_allowed_user_ids() -> frozenset[int]:
    """User IDs allowed to talk to the bot (comma-separated in .env)."""
    # LEARN: Returns a frozenset[int] — deduplicates and gives fast membership tests (``in``).
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


def resolve_vault_path() -> Path | None:
    """Resolve an optional external Obsidian vault for reference lookups.

    Returns the configured ``OBSIDIAN_VAULT_PATH`` if it exists, else ``None``.
    Kara's memory no longer depends on this; it's purely an optional read/write
    bridge to an external vault if you want it.
    """
    # LEARN: ``Path | None`` is a type hint meaning "Path object or None".
    # expanduser() turns ~/vault into the user's home directory path.
    configured = os.getenv("OBSIDIAN_VAULT_PATH", "").strip().strip('"').strip("'")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return candidate
    return None


def ensure_brain() -> None:
    """Create the brain directory tree if it doesn't exist yet."""
    # LEARN: mkdir(parents=True, exist_ok=True) creates nested folders without error if they exist.
    for d in (BRAIN_DIR, CORE_DIR, LEARNINGS_DIR, SESSIONS_DIR, INDEX_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


# LEARN: Calling a function at module import time runs setup as soon as config is imported.
ensure_brain()
