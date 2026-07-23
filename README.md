# Kara — Local Personal AI Agent

Kara is a local personal assistant powered by **Ollama** with a self-contained
local "brain". Talk to her via **CLI** or **Telegram**.

## The brain

Everything Kara knows lives in `personal_agent/brain/` (gitignored, all markdown
except the derived vector index):

```
brain/
  core/          Always-in-context working memory (persona / human / active_task)
  learnings/     Durable facts & insights (semantic memory)
  sessions/      Episodic logs of past conversations
  index/         Derived vector store (embeddings cache)
  settings.json  Active Ollama model (persists across restarts)
  state.db       Conversation history (SQLite, survives gateway restarts)
  logs/          gateway.log
```

## Ollama provider

Kara uses Ollama for **both chat and embeddings** today. The provider layer is a
small ``ChatProvider`` abstraction (see ``provider_base.py``) so additional
backends (Gemini, OpenAI API, OpenAI Codex OAuth) can be added without
rewriting the agent core.

| Mode | Host | API key |
|---|---|---|
| **Cloud** (recommended with your key) | `https://ollama.com` | `OLLAMA_API_KEY` from [ollama.com/settings/keys](https://ollama.com/settings/keys) |
| **Local** | `http://localhost:11434` | not required |

When `OLLAMA_API_KEY` is set, Kara defaults to the cloud host. Leave it blank to
use a locally running Ollama daemon instead.

Provider config lives in ``brain/providers.json`` (auto-seeded from ``.env``).
Runtime adapters are built via ``providers.to_chat_provider()``; Ollama is
implemented in ``providers_ollama.py``.

## Setup

1. Install deps:

```bash
cd personal_agent
uv sync
```

2. Configure `.env`:

```bash
cp .env.example .env
```

Add your Ollama API key:

```
OLLAMA_API_KEY=your_key_here
OLLAMA_MODEL=gpt-oss:120b
```

3. Run (CLI):

```bash
uv run agent.py
```

For **local** Ollama instead, install [Ollama](https://ollama.com/download) and
pull models:

```bash
ollama pull llama3.3
ollama pull nomic-embed-text
```

Then leave `OLLAMA_API_KEY` blank in `.env`.

## Multiple providers / API keys

Kara supports **multiple Ollama API keys**. Each key becomes a provider, and
``/models`` queries all of them and lists available models per provider.

Add keys to ``.env``:

```env
OLLAMA_API_KEY=your_primary_key
OLLAMA_API_KEY_WORK=your_work_key
OLLAMA_API_KEY_PERSONAL=your_personal_key
```

Provider definitions are stored in ``brain/providers.json`` (auto-created from
``.env``). API keys always stay in ``.env`` — never in the JSON file.

Commands:

- ``/providers`` — list provider IDs and native switch commands
- ``/provider <provider-id>`` — switch provider using its default model
- ``/provider <provider-id> <model>`` — switch provider and model together
- ``/models`` — all providers + models from each (live from API)
- ``/model`` — models for the active provider only
- ``/model <name>`` — switch model on active provider
- ``/model <provider-id>/<model>`` — switch provider and model in one command

## Switching providers and models

- `/providers` — list provider IDs and switch commands
- `/provider ollama-cloud` — switch to Ollama Cloud using its default model
- `/provider openai-codex` — switch to OpenAI Codex using its default model
- `/provider ollama-cloud gpt-oss:20b` — switch provider and model together
- `/models` — list every provider and its models
- `/model` — list models for the active provider
- `/model gpt-oss:20b` — switch model on the active provider
- `/model openai-codex/gpt-5.5` — switch provider and model in one command

Switching resets the in-chat context for that session; brain memory is unchanged.

## Gateway (24/7 Telegram, Hermes-style)

One long-lived process runs Telegram + the agent core. Conversations persist in
**SQLite** (`brain/state.db`) across restarts.

### Setup

1. Configure Telegram in `.env` (see above)
2. **Install for PC startup (no terminal window):**

```powershell
cd personal_agent
uv run install_gateway.py
```

This registers a Windows Scheduled Task (`KaraGateway`) that runs at logon using
`pythonw.exe` — no console window.

Start immediately without reboot:

```powershell
schtasks /Run /TN KaraGateway
```

Remove startup task:

```powershell
uv run install_gateway.py --uninstall
```

### Manual run (with terminal, for debugging)

```powershell
uv run gateway.py
```

### Updates

When you pull new code or change dependencies:

```powershell
uv run update.py
```

This runs `git pull`, `uv sync`, and signals the gateway to **restart itself**
gracefully (picks up new code). You can also send `/restart` in Telegram.

The gateway also auto-restarts when it detects Python source changes (every ~10s).

### Logs & state

| Path | Purpose |
|---|---|
| `brain/state.db` | Conversation history (SQLite) |
| `brain/logs/gateway.log` | Gateway log file |
| `brain/gateway.pid` | Running gateway PID |
| `brain/restart.flag` | Pending restart signal |

Telegram commands: `/start`, `/models`, `/model`, `/model <name>`, `/new`, `/restart`

### Gateway tuning (optional `.env`)

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_POLL_INTERVAL` | `10` | Seconds between restart/update checks |

## Configuration (`.env`)

| Variable | Purpose | Default |
|---|---|---|
| `OLLAMA_API_KEY` | Ollama cloud API key | (local mode if blank) |
| `OLLAMA_HOST` | Ollama API host | cloud if key set, else localhost |
| `OLLAMA_MODEL` | Default chat model | `gpt-oss:120b` |
| `EMBED_MODEL` | Embedding model for memory search | `nomic-embed-text` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | (disabled) |
| `TELEGRAM_ALLOWED_USER_IDS` | Allowed Telegram user ids | (required for bot) |
| `SEARXNG_URL` | Preferred SearXNG search instance | `https://search.ameera.dev` |
| `WEB_SEARCH_FALLBACK_URL` | Public HTML search fallback when SearXNG is unavailable | Brave Search |
| `WEB_SEARCH_SECONDARY_FALLBACK_URL` | Secondary public HTML fallback | DuckDuckGo HTML |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token id | (optional) |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret | (optional) |
| `OBSIDIAN_VAULT_PATH` | Optional external Obsidian vault | (disabled) |
| `KARA_FILE_READ_ROOTS` | `;`-separated roots allowed for file read/search on Windows | project + user home |
| `KARA_FILE_WRITE_ROOTS` | `;`-separated roots allowed for file writes on Windows | project only |
| `KARA_ALLOW_SENSITIVE_FILES` | Allow credential/profile paths such as `.ssh`, `.codex`, and `.env` | `0` |
| `KARA_FILE_SEARCH_TIMEOUT` | Maximum seconds spent on one PC file search | `12` |
| `KARA_CUA_ENABLED` | Enable the installed `cua-driver` adapter | `1` |
| `KARA_CUA_DRIVER_CMD` | Override the `cua-driver` executable path | found on `PATH` |
| `KARA_CUA_TELEMETRY` | Opt in to cua-driver telemetry | `0` |
| `KARA_CUA_FOCUS_SETTLE_SECONDS` | Brief delay before foreground verification | `0.2` |

Semantic memory uses hybrid search (embeddings + keywords). If embeddings fail,
`search_memory` falls back to keyword search.

## Local files, Office documents, SQLite, Python, Windows, and computer use

Kara exposes native file tools as normal model-callable functions. Relative paths
resolve against `personal_agent/`. Read/search access includes the user's home
folder by default; writes remain in the project unless `KARA_FILE_WRITE_ROOTS`
is deliberately expanded. Common credential stores and application profiles are
blocked unless `KARA_ALLOW_SENSITIVE_FILES=1` is explicitly set.

### General files

| Tool | Capability |
|---|---|
| `list_directory` / `search_files` | Bounded local discovery and text search |
| `read_file` / `file_info` | Bounded text reads and structured metadata |
| `write_file` | Explicit UTF-8 file creation, overwrite, or append |
| `copy_file` | Copy a file into a write root; refuses implicit overwrite |
| `move_file` | Move/rename only when both paths are in write roots |
| `replace_in_file` | Exact text replacement only when the expected match count agrees |

### Microsoft Office files

Office tools create and modify real OOXML files directly; Microsoft Office does
not need to be open.

| Tool | Capability |
|---|---|
| `read_office_file` | Extract bounded text/rows from `.docx`, `.xlsx`, and `.pptx` |
| `create_word_document` / `append_word_text` | Create and append Word paragraphs |
| `create_excel_workbook` / `set_excel_cell` | Create typed workbooks from CSV and update one A1 cell |
| `create_powerpoint` / `append_powerpoint_slide` | Create a styled deck from JSON and append slides |

Created Office packages are reopened after saving so corrupt output is reported
rather than presented as successful.

### SQL and Python files

Plain `.sql` and `.py` source can be read, written, copied, moved, or patched by
the general tools. Format-aware tools add safe inspection:

| Tool | Capability and boundary |
|---|---|
| `inspect_sqlite_database` | Tables, views, columns, indexes, and create SQL from local SQLite files |
| `query_sqlite_database` | One bounded read-only query; URI `mode=ro`, `query_only`, authorizer, and timeout enforced |
| `inspect_python_file` | AST imports, functions, classes, methods, docstrings, and syntax; never executes code |
| `validate_python_file` | Compile-only syntax validation with exact diagnostics; never executes code |
| `run_python_tests` | Fixed `unittest discover`, write-root only, secret-stripped child environment, bounded output/timeout, exact two-turn approval |

The SQLite query tool cannot insert, update, delete, attach, migrate, or alter a
database. Python test execution accepts no shell command and cannot run until the
user replies with the exact generated approval phrase in a later message.

Web search is available through `web_search` (preferred SearXNG, then public
Brave Search / DuckDuckGo HTML fallbacks) and `web_fetch`.

Kara also has a native, read-only Windows operations toolkit:

| Tool | Inspects |
|---|---|
| `system_overview` | Windows version, machine model, memory, CPU count, and boot time |
| `list_processes` | Process names, PIDs, parent PIDs, memory use, and executable paths |
| `list_services` | Service names, status, startup mode, and owning PID |
| `list_scheduled_tasks` | Task names, paths, state, principal, action count, and trigger count |
| `disk_usage` | Fixed-drive capacity, used space, free space, labels, and filesystems |

These tools run fixed PowerShell inventory scripts and apply user-provided filters
inside Python, preventing filter text from becoming shell code. They cannot stop
processes, modify services, edit tasks, or change disks.

If `cua-driver` is installed (Hermes installs the same driver), Kara also exposes
one `computer_use` tool. `list_apps`, `list_windows`, and accessibility-tree
`capture` calls are read-only. Click, typing, key, scroll, and focus actions use
a two-message approval: Kara returns `approve <token>`, and only an exact reply
from the user can authorize that exact action against the resolved PID, HWND,
title, and app identity. Keyboard actions foreground and verify that same window
internally, then use foreground delivery; Chromium/Electron background failures
for pointer/scroll actions are retried the same way without asking for a second
approval. Screenshots are deliberately not fed back as base64 text; the
Codex/Ollama model operates on the driver's numbered accessibility elements
instead.
