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
  scheduler.db   Durable reminders and autonomous recurring jobs
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
| `KARA_SCHEDULER_POLL_SECONDS` | `15` | Seconds between durable-job checks |

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
| `KARA_DOCUMENT_MAX_BYTES` | Maximum PDF/image input size | `52428800` |
| `KARA_DOCUMENT_MAX_CHARS` | Maximum extracted text returned per call | `50000` |
| `KARA_PDF_MAX_PAGES` | Maximum PDF pages returned per call | `50` |
| `KARA_OCR_MAX_IMAGE_PIXELS` | Maximum accepted image pixel count | `40000000` |
| `KARA_OCR_TIMEOUT_SECONDS` | Maximum local OCR execution time | `30` |
| `KARA_PDF_TIMEOUT_SECONDS` | Maximum total PDF worker execution time | `60` |
| `KARA_PDF_WORKER_MEMORY_MB` | Maximum aggregate memory for the PDF/OCR worker process tree | `512` |
| `KARA_CUA_ENABLED` | Enable the installed `cua-driver` adapter | `1` |
| `KARA_CUA_DRIVER_CMD` | Override the `cua-driver` executable path | found on `PATH` |
| `KARA_CUA_TELEMETRY` | Opt in to cua-driver telemetry | `0` |
| `KARA_CUA_FOCUS_SETTLE_SECONDS` | Brief delay before foreground verification | `0.2` |
| `KARA_TIMEZONE` | Default IANA timezone for reminder and cron tools | `Europe/Dublin` |
| `KARA_SCHEDULER_POLL_SECONDS` | Scheduler polling interval in seconds | `15` |
| `GITHUB_CLIENT_ID` | GitHub OAuth App client id (Device Flow enabled) | (required for GitHub tools) |
| `GITHUB_OAUTH_SCOPES` | Space-separated OAuth scopes requested at login | `repo workflow gist read:org notifications` |
| `GITHUB_GIT_TIMEOUT` | Max seconds for a single `git` subprocess call | `120` |
| `MNEMOSYNE_BIN` | Path/command for the Mnemosyne CLI | `mnemosyne` (must be on `PATH`) |
| `MNEMOSYNE_DB_PATH` | Optional override for Mnemosyne's SQLite database path | (Mnemosyne's own default) |

Semantic memory uses hybrid search (embeddings + keywords). If embeddings fail,
`search_memory` falls back to keyword search.

## Reminders and autonomous scheduled jobs

Kara has a durable scheduler backed by `brain/scheduler.db`. Jobs survive gateway
and Windows restarts and are delivered back to the authenticated Telegram chat
that created them. A hidden runtime clock is also regenerated immediately before
every model request, so Kara always receives the current local and UTC datetime,
day, timezone, and UTC offset without displaying or persisting that metadata.

| Tool | Capability |
|---|---|
| `schedule_reminder` | Save exact text and deliver it directly without an LLM run |
| `schedule_agent_job` | Start a fresh unattended Kara run and deliver its result |
| `list_scheduled_jobs` | List only the current authenticated user's jobs |
| `pause_scheduled_job` / `resume_scheduled_job` | Stop or resume future runs |
| `run_scheduled_job_now` | Queue an owned job for the next polling cycle |
| `delete_scheduled_job` | Permanently remove an owned job |

Schedules accept an offset-aware ISO timestamp, a strict relative delay such as
`in 15m` or `in 2h`, or a standard five-field cron expression such as
`0 8 * * *`. Cron is evaluated in the supplied IANA timezone and therefore
follows daylight-saving changes.

Autonomous jobs have a separate safety boundary: they can use only observational
web, memory-search, file-read, Office-read, SQLite-read, Python-inspection, and
Windows-inventory tools. They cannot write files, send email, drive the desktop,
execute tests, mutate memory, or recursively create more scheduled jobs. A job
interrupted by a gateway restart is recovered with at-least-once delivery.

## GitHub

Kara talks to GitHub over the REST API, authenticated with a real **OAuth App
+ Device Flow login** — not a personal access token (fine-grained or classic).
The device flow needs no client secret and no redirect webserver, so it fits
a CLI/Telegram agent cleanly.

### Setup

1. On github.com: **Settings → Developer settings → OAuth Apps → New OAuth App**.
   Homepage/callback URL can be anything (`http://localhost` works); device
   flow doesn't use them.
2. Open the new app's settings and check **Enable Device Flow**.
3. Copy the **Client ID** into `.env`:

```
GITHUB_CLIENT_ID=your_client_id_here
```

4. Log in:

```bash
uv run python github_auth.py login
```

Follow the printed URL + code, approve in the browser. Tokens are stored in
`brain/auth.json` (gitignored), never in `.env`. Check status any time with
`uv run python github_auth.py status`, or ask Kara to run `github_status`.

### Scopes

Default scope is `repo workflow gist read:org notifications` — full access to
your private and public repos, Actions, gists, org read, and notifications.
Override with `GITHUB_OAUTH_SCOPES` in `.env` before logging in (e.g. drop to
`public_repo` for public-only access).

### Tools

| Tool | Capability |
|---|---|
| `github_status` | Connection state, granted scopes, rate limit |
| `github_search_repositories` / `github_get_repository` | Search and inspect repos |
| `github_list_repository_contents` / `github_read_repository_file` | Browse and read files without cloning |
| `github_search_code` | Code search, repo-scoped or global |
| `github_list_branches` / `github_list_commits` | Branch and commit history |
| `github_list_issues` / `github_get_issue` / `github_list_issue_comments` / `github_search_issues` | Read issues and PR/issue search |
| `github_list_pull_requests` / `github_get_pull_request` / `github_get_pull_request_diff` / `github_list_pull_request_files` | Read pull requests and diffs |
| `github_list_workflow_runs` / `github_get_workflow_run` | Actions/CI status |
| `github_list_notifications` | Unread (or all) notifications |
| `github_create_issue` / `github_comment_on_issue` / `github_close_issue` | Write to issues — **approval-gated** |
| `github_create_pull_request` / `github_merge_pull_request` | Open/merge PRs — **approval-gated** |
| `github_star_repository` | Star a repo — **approval-gated** |
| `git_clone_repository` / `git_pull_repository` | Clone/pull into an allowed write root using the OAuth token as an ephemeral, never-persisted git credential |
| `git_push_changes` | Stage, commit, and push — **approval-gated** |

All actions that publish something (issues, comments, PRs, merges, stars, git
pushes) use the same exact two-turn approval pattern as `run_python_tests`:
Kara requests approval, shows a one-time phrase, and only proceeds once you
reply with that exact phrase in your next message. Read tools need no approval.
`git_clone_repository` / `git_pull_repository` stay inside `KARA_FILE_WRITE_ROOTS`
like the rest of Kara's file tools.

## Mnemosyne (optional external memory, over MCP)

[Mnemosyne](https://github.com/mnemosyne-oss/mnemosyne) is a separate,
SQLite-backed, three-tier memory system for AI agents. Kara can talk to it
as an MCP server, purely as an **additional, optional** memory surface — it
does not replace or share data with Kara's own core/learnings/session
memory (`tools/memory_tools.py`).

### Setup

Install it as a real dependency of Kara's own project (from `personal_agent/`),
not via a bare `pip install` in some other terminal/Python — the gateway only
sees packages inside its own `.venv`:

```bash
uv add "mnemosyne-memory[mcp,embeddings]"
```

Skip the `[all]` extra unless you have a C/C++ build toolchain installed — it
pulls in `llama-cpp-python`, which compiles from source (CMake + `nmake`/MSVC)
and will fail on a plain machine. `[mcp,embeddings]` gets the full MCP server
plus real vector search (via `fastembed` + `sqlite-vec`, prebuilt wheels, no
compiler needed) — the `[llm]` extra is for a separate local-LLM feature
Kara's tools don't use.

That's it — `tools/mnemosyne_tools.py` spawns `mnemosyne mcp` (stdio) itself
the first time a Mnemosyne tool is called; there's no separate process to
manage. Executable discovery checks, in order: an absolute `MNEMOSYNE_BIN`
path, the venv this exact interpreter is running from (`sys.prefix`), then a
`PATH` search. The middle step matters because the gateway is launched by
directly invoking `.venv/Scripts/python.exe` (Task Scheduler → a `.vbs`
launcher, or the gateway's own self-restart) — that never puts `.venv/Scripts`
on `PATH`, so a `PATH`-only lookup works in a dev shell (`uv run ...`) but
silently fails for the real running gateway even though the package is
correctly installed. Only set `MNEMOSYNE_BIN` in `.env` if the executable
genuinely lives outside this venv.

If the gateway was already running when you installed the dependency, restart
it (`schtasks /Run /TN KaraGateway`, or let it pick up the change automatically
on its next ~10s source-change poll — though a fresh `.venv` package isn't a
source-file change, so a manual restart is safer here).

### Tools

| Tool | Capability |
|---|---|
| `mnemosyne_status` | Whether Mnemosyne is installed/reachable, and its live MCP tool inventory |
| `mnemosyne_remember` | Store a memory (optionally tagged) |
| `mnemosyne_recall` | Search stored memories |
| `mnemosyne_call_tool` | Call any other Mnemosyne MCP tool by exact name (knowledge-graph, multi-agent, working-note, operational) — Mnemosyne's own docs call its full tool surface version-specific, so this is a generic escape hatch rather than a hardcoded list |

The bridge (`tools/mcp_bridge.py`) is a generic stdio MCP client — one
background thread keeps a persistent session open to any MCP server
subprocess and exposes ordinary blocking `list_tools()`/`call_tool()` calls,
so future MCP servers can be wired in the same way.

## Local files, documents/OCR, SQLite, Python, Windows, and computer use

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

### PDF and image OCR

PDF and OCR tools are observational and use the same configured file-read roots
and sensitive-path policy as Kara's other file tools. Files remain local: PDF
text is extracted with PyMuPDF, while image-only PDF pages and screenshots use
the Windows Media OCR engine already included with Windows 10/11.

| Tool | Capability |
|---|---|
| `read_pdf` | Extract bounded embedded text from `.pdf`; OCR scanned or nearly textless pages automatically |
| `ocr_image` | Extract local text from `.png`, `.jpg`/`.jpeg`, `.bmp`, `.tif`, and `.tiff` |

The tools enforce file-size, page-count, image-pixel, response-character, and
OCR-time limits. They do not upload documents, modify source files, bypass PDF
passwords, or promise perfect handwriting/layout recognition. Large PDFs can be
paged with `start_page` and `max_pages`.

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
