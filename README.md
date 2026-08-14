# Kara — Local Personal AI Agent

Kara is a local-first personal AI agent for CLI and Telegram. They combine configurable chat providers with persistent memory, local PC/file tools, scheduling, email, GitHub, and safety-gated desktop automation.

## What this is for

Kara exists to be an assistant that **remembers you across sessions and can act on your own machine, without that state living in someone else's cloud**. Three mechanisms carry that:

- **Memory is a directory you own.** Conversations go to SQLite at `brain/state.db`; durable facts go to Markdown in `brain/learnings/`. Semantic recall is built from those two sources, not from a hosted index. The whole `brain/` directory is gitignored — you can read it, back it up, or delete it.
- **The model is swappable from `.env` alone.** Ollama, ChatGPT Codex over OAuth, or any OpenAI-compatible endpoint. Every adapter returns the same `ChatResult`, so nothing above the provider layer knows which backend answered, and switching costs no code.
- **Acting on your machine is gated, not assumed.** Reads and inspection are the default. Writing files, sending email, running tests, driving the desktop, and publishing to GitHub each sit behind a distinct gate, and the riskiest of them require a two-turn approval token bound to the exact action.

It is a single-user personal agent, not a multi-tenant service or a framework: sessions are keyed per Telegram user, and the safety model assumes the person running it owns the machine.

> **Privacy and safety:** Kara’s runtime state, credentials, provider tokens, local memory, and `.env` are intentionally machine-local and gitignored. Read-only inspection is the default; publishing, test execution, and desktop input require explicit approval.

## What Kara can do

- Chat through configurable providers: Ollama, OpenAI Codex OAuth, and any OpenAI-compatible endpoint (OpenAI, Groq, OpenRouter, DeepSeek, vLLM, LM Studio) configured from `.env` alone.
- Run continuously through a Telegram gateway with SQLite-backed conversation history.
- Maintain a local brain: always-in-context core memory, durable learnings, session logs, and hybrid semantic/keyword recall.
- Use optional Mnemosyne MCP memory as an additional structured memory surface.
- Search and fetch the web through public providers by default, or your own SearXNG instance if you configure one.
- Read, search, and explicitly write local files inside configured filesystem roots.
- Create/read/edit Word, Excel, and PowerPoint files without opening Microsoft Office.
- Extract text from PDFs and images locally using PyMuPDF plus Windows OCR or Tesseract.
- Inspect SQLite databases and Python source safely; run a bounded unittest suite only after approval.
- Read email through Himalaya; sending is separately disabled by default.
- Inspect Windows system/process/service/task/disk state without modifying it.
- Inspect or control desktop apps through `cua-driver`, with exact two-turn approval for input.
- Create durable reminders and scheduled autonomous read-only jobs.
- Read GitHub repositories, commits, issues, PRs, Actions, and notifications; publishing actions require approval.
- Search, read, and write notes in an external Obsidian vault, when one is configured.

## Architecture

```text
CLI / Telegram
     │
     ▼
KaraSession ── provider chat + tool loop ── tool registry
     │                                      │
     ├── SQLite conversation state           ├── local files/documents/OCR
     ├── local brain + vector index          ├── web/email/GitHub/MCP
     ├── scheduler                           └── system/computer inspection
     └── optional Mnemosyne MCP
```

`kara.py` owns the main model → tool → model loop. Tool schemas are generated from Python function signatures and docstrings. Telegram handlers run blocking provider, SQLite, and tool work in worker threads so the event loop remains responsive.

### Module map

```text
agent.py            CLI entry point            main.py             Launcher (CLI/gateway/update/install)
kara.py             KaraSession + tool loop    config.py           Paths and .env settings

Providers
  provider_base.py  Provider interface, ChatResult, retry/backoff
  providers.py      Provider registry and discovery from .env
  models.py         Active model/provider selection, /models formatting
  providers_ollama.py · providers_codex.py · providers_openai_compatible.py
  ollama_client.py  Shared Ollama HTTP client (chat, embeddings, model list)

Memory and context
  memory_store.py   Core memory + learnings (Markdown)
  session_db.py     SQLite: messages, sessions, session_summaries, turns
  vector_index.py   Local hybrid index over learnings + summaries
  embeddings.py     Embeddings via the active provider
  context_budget.py Token accounting and compaction thresholds

Scheduling and time
  scheduler.py      Durable job storage (brain/scheduler.db)
  scheduled_runner.py  Executes due jobs and delivers results
  time_context.py   Per-request clock injected into the prompt

Auth
  auth_store.py     Shared token store (brain/auth.json)
  codex_auth.py · github_auth.py   Device-code OAuth flows

Tools
  tools/registry.py Aggregates every module's TOOL_GROUP/TOOLS/SCHEDULED_SAFE
  tool_schemas.py   Function signature + docstring -> JSON schema
  tools/<group>_tools.py            one file per group, each declaring its own
                   TOOL_GROUP/TOOLS/SCHEDULED_SAFE/READ_ONLY:
                   memory · web · file · document · office · sql · python ·
                   computer · windows · scheduler · email · github ·
                   mnemosyne · obsidian
  tools/http_client.py  Shared pooled HTTP client
  tools/mcp_bridge.py   stdio MCP client (used by the Mnemosyne bridge)

Gateway
  gateway/run.py    Long-lived daemon      gateway/sessions.py  Session cache
  gateway/commands.py  Slash commands      gateway/restart.py   Restart + update detection
  gateway/platforms/telegram.py       Telegram adapter
  gateway/platforms/tg_format.py      Markdown -> Telegram-safe HTML

Ops
  update.py · install_gateway.py · scripts/
```

### Tool registry and on-demand loading

Each module in `tools/` declares its own `TOOL_GROUP`, `TOOLS`, and `SCHEDULED_SAFE` set. `tools/registry.py` aggregates those declarations into the tool registry, the JSON schemas, and the scheduled-run allowlist, so adding a tool means editing one file.

Groups are also the unit of loading. A session starts with the always-on groups — memory, web, file, and document — and reveals the rest when the request needs them, either from keywords in the message or when the model calls `activate_tool_group`. Exposing all 84 tools costs about 40KB of schema per request; a typical request now carries roughly 8.8KB. Once activated, a group stays available for the remainder of the session.

Loading is a presentation filter only. Scheduled jobs are bounded by `allowed_tool_names`, which remains the execution boundary and is never widened by group activation.

## Local brain

Everything private and persistent lives in `brain/` and is gitignored:

```text
brain/
  core/          Always-in-context persona, user, and active-task blocks
  learnings/     Durable facts and decisions (Markdown)
  sessions/      Legacy conversation logs, retained but no longer written or indexed
  index/         Derived vector index for hybrid search (index.json)
  settings.json  Active provider/model settings
  providers.json Provider definitions without API keys
  state.db       SQLite conversation history, session summaries, and per-turn usage
  scheduler.db   Durable reminders and scheduled jobs
  auth.json      OAuth tokens (GitHub/Codex), when configured
  logs/          Gateway logs (gateway.log, gateway_boot.log)
```

The gateway also keeps its coordination state here: `gateway.pid`,
`gateway.instance.lock` (single-instance guard), `restart.flag`, `restart.lock`,
`restart_notify.json` (pending results to deliver after a restart),
`code.fingerprint` (source-change detection), and `sessions_migrated.marker`.
All of it is disposable — deleting `brain/` costs you memory and history, not
the ability to start.

Conversation transcripts live in one place: the `messages` table in `state.db`, alongside `sessions` and a `turns` table that records per-turn token usage, tool calls, and duration (this is what `/usage` reports). Semantic recall is built from two curated sources instead — `learnings/` (facts Kara chose to save) and the `session_summaries` table (one recap per finished conversation). Raw turns are deliberately not embedded, so recall returns decisions rather than small talk, and the index no longer grows with every scheduled job that runs.

Existing `brain/sessions/*.md` logs are migrated once on startup: any `## Summary` block is lifted into `session_summaries` and the files are left on disk untouched.

Kara manages this memory through five always-on tools:

| Tool | Effect |
|---|---|
| `core_memory_append` | Add a line to a core block (`persona`, `human`, `active_task`) |
| `core_memory_replace` | Correct an existing line in a core block |
| `set_active_task` | Set or clear what Kara is currently working on |
| `save_learning` | Write a durable fact or decision to `learnings/` as Markdown |
| `search_memory` | Hybrid semantic + keyword recall over learnings and session summaries |

Kara’s built-in semantic search uses cached hybrid ranking: embeddings plus keyword matching. A cheap fingerprint — file stat for learnings, row count and latest id for summaries — avoids re-embedding when nothing has changed. If embeddings are unavailable, search falls back to keywords.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python **3.14+** (`uv` can install and manage it)
- One chat provider: local Ollama, Ollama Cloud, or OpenAI Codex OAuth
- Optional: Windows 10/11, a supported Linux desktop, or macOS for `cua-driver` desktop support
- Optional image OCR: built-in Windows OCR, or Tesseract (`tesseract-ocr` on Linux; `brew install tesseract` on macOS)

## Quick start: clone and run Kara

### 1. Clone the repository

```shell
git clone https://github.com/odaiameera/Kara-Personal-Agent.git
cd Kara-Personal-Agent
```

GitHub CLI users can instead run:

```shell
gh repo clone odaiameera/Kara-Personal-Agent
cd Kara-Personal-Agent
```

### 2. Install Python and dependencies

Install `uv` using its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/), then run:

```shell
uv python install 3.14
uv sync
```

`uv sync` creates an isolated `.venv` and installs Kara's locked dependencies. You do not need to activate the environment; use `uv run ...` for commands below.

### 3. Create your local configuration

Linux/macOS:

```shell
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

`.env` is gitignored. Never commit it.

### 4. Configure a model provider

Choose one of these options.

#### Option A: local Ollama

Install [Ollama](https://ollama.com/download), make sure its server is running, and download a chat model plus the default embedding model:

```shell
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

Set these values in `.env`:

```env
OLLAMA_API_KEY=
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
EMBED_MODEL=nomic-embed-text
```

You can substitute another Ollama model that supports the tools and context size you need.

#### Option B: Ollama Cloud

```env
OLLAMA_API_KEY=your_real_ollama_api_key
OLLAMA_HOST=https://ollama.com
OLLAMA_MODEL=gpt-oss:120b
EMBED_MODEL=nomic-embed-text
```

#### Option C: OpenAI Codex OAuth

Complete the device login, then select the provider from Kara with `/provider openai-codex`:

```shell
uv run python codex_auth.py login
```

### 5. Start the CLI

```shell
uv run python agent.py
```

On first use Kara creates their private runtime state under `brain/`. Type `/providers` to inspect available providers, `/models` to list models, and `/new` to begin a fresh conversation without deleting long-term memory.

### 6. Optional: connect Telegram

1. Create a bot with Telegram's `@BotFather` and copy its token.
2. Message `@userinfobot` to find your numeric Telegram user ID.
3. Add both values to `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ALLOWED_USER_IDS=123456789
```

Multiple allowed users can be comma-separated. Start the gateway in the foreground first so configuration errors are visible:

```shell
uv run python -m gateway.run
```

Only IDs in `TELEGRAM_ALLOWED_USER_IDS` can use the bot.

### 7. Optional: enable desktop control and OCR

- Install [Cua Driver](https://cua.ai/docs/how-to-guides/driver/install), then run `cua-driver doctor` to verify desktop and accessibility permissions. Kara's CLI and Telegram features work without it.
- On Linux, install Tesseract with your distribution package manager (for example `sudo apt install tesseract-ocr` on Debian/Ubuntu).
- On macOS, install Tesseract with `brew install tesseract`; Cua Driver also needs Accessibility and Screen Recording permission.
- Windows uses its built-in OCR engine and does not require Tesseract.

### 8. Verify the installation

```shell
uv run python -m unittest discover -s tests
```

Real OCR integration tests skip when no supported OCR backend is installed.

## Providers and models

Kara uses a small provider abstraction, so provider-specific authentication does not leak into the agent loop. Every adapter returns a `ChatResult` — content, tool calls, token usage, finish reason — so nothing above the provider layer knows which backend answered.

Three adapters cover the field:

| Type | Backends |
|---|---|
| `ollama` | Ollama Cloud and local Ollama |
| `openai-codex` | ChatGPT Codex over OAuth |
| `openai-compatible` | Anything speaking OpenAI `/chat/completions` — OpenAI, Groq, Together, OpenRouter, DeepSeek, Mistral, Fireworks, vLLM, LM Studio |

Adding a backend of the third kind needs no code. Set a base URL in `.env` and Kara discovers it:

```shell
KARA_PROVIDER_GROQ_BASE_URL=https://api.groq.com/openai/v1
KARA_PROVIDER_GROQ_API_KEY=gsk_...
KARA_PROVIDER_GROQ_MODEL=llama-3.3-70b-versatile
```

That becomes the provider `groq`, usable via `/provider groq`. `KARA_PROVIDER_<NAME>_API_KEY` is optional — local servers need none.

Telegram/CLI commands include:

- `/providers` — list configured providers
- `/provider <id> [model]` — switch provider, optionally with a model
- `/usage` — tokens, tool calls and time for this session and today
- `/context` — how full the context window is and when compaction starts
- `/stop` — cancel a turn that is still running
- `/models` — list models across providers
- `/model [provider/model-or-name]` — inspect or switch model
- `/new` — start a fresh chat while preserving long-term memory
- `/restart` — request a graceful gateway restart
- `/auth codex` — how to complete the Codex device login (it runs in a local terminal, not over Telegram)
- `/codex-status` — confirm stored Codex credentials

In the CLI, `exit` or `quit` ends the session and writes its summary.

Switching provider or model resets only the current chat context, not the brain.

## 24/7 Telegram gateway on Windows

Install the Windows logon task:

```powershell
uv run install_gateway.py
```

This creates the `KaraGateway` Scheduled Task and launches the gateway without a console window. For debugging:

```powershell
uv run python -m gateway.run
```

The gateway auto-restarts after source changes, persists conversations in `brain/state.db`, and delivers pending scheduler results after restarts. Regular chat replies render Markdown as Telegram HTML; malformed formatting falls back to plain text. Commands intentionally remain plain text.

### Linux and macOS

There is no auto-start integration yet — `install_gateway.py` exits with "This installer is for Windows only," and everything in `scripts/` is PowerShell, `.cmd`, or VBScript. Run the gateway in the foreground instead:

```shell
uv run python -m gateway.run
```

The daemon itself is already cross-platform: `gateway/restart.py` handles POSIX process spawning and liveness checks, and only special-cases Windows for the hidden console window. What's missing is the service registration that starts it at login or boot and brings it back if it dies. See [Roadmap](#roadmap).

## Configuration

Copy `.env.example` and set only the integrations you use. Never commit `.env`.

| Area | Key settings |
|---|---|
| Identity | `KARA_USER_NAME` — what Kara calls you; defaults to "the user" |
| Providers | `OLLAMA_API_KEY`, `OLLAMA_HOST`, `OLLAMA_MODEL`, `EMBED_MODEL` |
| Any OpenAI-compatible backend | `KARA_PROVIDER_<NAME>_BASE_URL`, `_API_KEY`, `_MODEL` |
| Turn limits | `KARA_MODEL_CONTEXT_TOKENS`, `KARA_MAX_TOOL_ITERATIONS`, `KARA_TURN_TIMEOUT_SECONDS`, `KARA_MAX_REPEATED_TOOL_CALLS` |
| Context compaction | `KARA_COMPACT_AT_FRACTION`, `KARA_MAX_TOOL_RESULT_CHARS` |
| Provider resilience | `KARA_PROVIDER_RETRY_ATTEMPTS`, `KARA_PROVIDER_RETRY_BASE_DELAY`, `KARA_PROVIDER_TIMEOUT_SECONDS` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` |
| Web search | `SEARXNG_URL` (optional), Cloudflare Access variables, public fallback URLs |
| Files | `KARA_FILE_READ_ROOTS`, `KARA_FILE_WRITE_ROOTS`, `KARA_ALLOW_SENSITIVE_FILES` |
| Documents | `KARA_DOCUMENT_MAX_BYTES`, `KARA_PDF_MAX_PAGES`, OCR/PDF time and memory limits |
| Desktop | `KARA_CUA_ENABLED`, `KARA_CUA_DRIVER_CMD`, `KARA_CUA_TELEMETRY` |
| Scheduling | `KARA_TIMEZONE` (defaults to UTC), `KARA_SCHEDULER_POLL_SECONDS` |
| GitHub | `GITHUB_CLIENT_ID`, `GITHUB_OAUTH_SCOPES`, `GITHUB_GIT_TIMEOUT` |
| Mnemosyne | `MNEMOSYNE_BIN`, `MNEMOSYNE_DB_PATH` |
| Obsidian | `OBSIDIAN_VAULT_PATH` |
| Email | Himalaya configuration (`HIMALAYA_BIN`, `HIMALAYA_CONFIG`, `HIMALAYA_ACCOUNT`) plus `EMAIL_SEND_ENABLED=true` only when deliberate |

`.env.example` is the exhaustive list and documents every option with its
default; the table above is a map of the categories.

Filesystem access is intentionally constrained. Reads/searches default to the project and user home; writes default to the project. Sensitive paths such as `.env`, credential stores, and application profiles are blocked unless explicitly enabled.

## Tools and boundaries

### Web and email

- `web_search` and `web_fetch` retrieve current public information.
- Himalaya-backed email tools can inspect folders, list/search/read messages, and mark messages read.
- Sending email is a separate safety gate and remains disabled unless `EMAIL_SEND_ENABLED=true` is set.

### Local files and documents

| Area | Tools |
|---|---|
| Files | `list_directory`, `search_files`, `read_file`, `file_info`, `write_file`, `copy_file`, `move_file`, `replace_in_file` |
| Office | `read_office_file`, `create_word_document`, `append_word_text`, `create_excel_workbook`, `set_excel_cell`, `create_powerpoint`, `append_powerpoint_slide` |
| PDFs/images | `read_pdf`, `ocr_image` |
| SQLite | `inspect_sqlite_database`, `query_sqlite_database` |
| Python | `inspect_python_file`, `validate_python_file`, `run_python_tests` |

Office files are manipulated directly as OOXML packages; Microsoft Office does not need to be open. Created packages are reopened after saving to verify they are valid. PDF and image extraction stays local and is bounded by file, page, pixel, timeout, and response limits.

SQLite access is read-only. Python inspection never executes source. `run_python_tests` is restricted to fixed `unittest discover` inside configured write roots and requires exact two-turn approval.

### System inspection and cross-platform desktop use

Read-only Windows inventory tools:

- `system_overview`
- `list_processes`
- `list_services`
- `list_scheduled_tasks`
- `disk_usage`

They use fixed PowerShell inventory scripts and cannot stop processes, change services, edit tasks, or alter disks.

Windows inventory remains Windows-only. `computer_use` is cross-platform through the installed `cua-driver` on Windows, Linux, and macOS. It lists apps/windows and captures accessibility elements; current driver snapshots expose opaque `element_token` handles and `snapshot_id` values, which should be preferred over a bare element index.

Clicks, typing, keys, scrolling, focus, and foregrounding require a fresh approval token bound to the exact target process/window/title. Input is delivered in the background first. Kara retries the same action with action-scoped foreground delivery only when the driver reports `background_unavailable` or a verified `suspected_noop` with a `foreground` recommendation. An `unverifiable` result is never repeated automatically, avoiding duplicate clicks or text. Persistent `bring_to_front` remains an explicit, separately approved action.

Linux requires a supported X11/Wayland accessibility and input setup. macOS requires Accessibility and Screen Recording permission for CuaDriver; inspect status with `cua-driver permissions status`. Run `cua-driver doctor` on any platform to diagnose missing permissions or desktop backends.

### Scheduling and live time

The scheduler is backed by `brain/scheduler.db`. Jobs survive gateway and machine restarts and are owned by the authenticated Telegram user who created them.

- `schedule_reminder` — deliver exact reminder text.
- `schedule_agent_job` — run an isolated autonomous agent task.
- `list_scheduled_jobs`, `pause_scheduled_job`, `resume_scheduled_job`, `run_scheduled_job_now`, `delete_scheduled_job` — manage owned jobs.

Schedules accept an offset-aware ISO timestamp, a strict relative delay such as `in 15m`, or five-field cron such as `0 8 * * *`. Cron schedules use the supplied IANA timezone and follow daylight-saving changes. A live local/UTC clock is injected into each model request.

Autonomous scheduled agent jobs are deliberately restricted to a fixed allowlist of 19 observational tools: web search/fetch, `search_memory`, file reads and search, `read_office_file`, SQLite inspection and queries, Python inspection/validation, Windows inventory, and Obsidian reads. They cannot write files, send email, drive the desktop, execute tests, mutate memory, or create jobs. PDF and OCR reads are also excluded, since they spawn bounded worker subprocesses.

That allowlist is assembled from each tool module's own `SCHEDULED_SAFE` declaration, and the registry refuses to start if a module marks a tool scheduled-safe without also marking it read-only.

## GitHub

GitHub uses OAuth App Device Flow—not a personal access token. Create an OAuth App, enable Device Flow, put its Client ID in `.env`, then authenticate:

```powershell
uv run python github_auth.py login
```

Tokens are stored in `brain/auth.json` and are gitignored. `github_status` reports connection status and granted scopes.

Read tools cover repositories, contents, branches, commits, code, issues, pull requests, Actions, and notifications. Clone/pull uses an ephemeral OAuth credential. Publishing actions—including issues, comments, PRs, merges, stars, and `git_push_changes`—require exact two-turn approval.

## Mnemosyne MCP memory (optional)

[Mnemosyne](https://github.com/mnemosyne-oss/mnemosyne) adds a separate SQLite-backed memory system alongside Kara’s own brain. It is optional and does not replace core memory, learnings, sessions, or the vector index.

Install it into Kara’s project environment:

```powershell
uv add "mnemosyne-memory[mcp,embeddings]"
```

Kara lazily starts `mnemosyne mcp` over stdio on first use. The bridge caches the MCP tool list for the connection, strips Kara credentials from the child environment while preserving `MNEMOSYNE_*` settings, and refuses ambiguous tool-name matches. `mnemosyne_status` shows the live server inventory; `mnemosyne_remember`, `mnemosyne_recall`, and `mnemosyne_call_tool` provide the base interface to Mnemosyne’s version-specific tool surface.

## Obsidian vault (optional)

Kara can read and write notes in an external [Obsidian](https://obsidian.md) vault. This is a plain bridge to a folder of Markdown — it is entirely separate from Kara's own brain, and nothing in core memory, learnings, or the vector index depends on it.

Point `OBSIDIAN_VAULT_PATH` at the vault directory in `.env`:

```env
OBSIDIAN_VAULT_PATH=/path/to/your/vault
```

The path is expanded (`~` works) and must exist; if it does not resolve, the three tools report that no vault is configured rather than failing:

- `search_obsidian` — search note contents across the vault
- `read_obsidian_note` — read one note
- `write_obsidian_note` — create or update a note

Vault writes are separate from the `KARA_FILE_WRITE_ROOTS` allow-list that governs the general file tools.

## Security model

- `.env`, `brain/`, virtual environments, and downloaded binaries are gitignored.
- Credentials are not passed to third-party subprocesses unless that subprocess requires its own explicitly scoped configuration.
- Local reads/writes are rooted and sensitive paths are blocked by default.
- Web content and downloaded text are never treated as permission to change local files.
- SQLite and Windows inventory are read-only.
- Email sending, test execution, desktop input, and GitHub publishing use separate safety gates.
- Approval tokens are one-time (consumed on use), expire after 10 minutes, and are bound to the originating session, the exact action and arguments, and the resolved target window. If the action, its arguments, or the target changes after approval was requested, the token is rejected and nothing runs.
- The approval phrase must be typed by the user: the gate compares the current user message against the exact string `approve <token>`, so the model cannot satisfy it by emitting the phrase itself.

## Development

The repository includes an extensive `unittest` suite for tools, auth, providers, scheduling, document artifacts, gateway behavior, and safety boundaries. Run it only in a trusted local development environment:

```powershell
uv run python -m unittest discover -s tests
```

Four tests currently fail under CPython 3.14.0rc2 — the `McpBridgeRealServerTests` cases, which spawn a real MCP server subprocess and hit a `prefer_fwd_module` incompatibility in the MCP SDK's pydantic use. They are unrelated to Kara's own code and pass on a release build of 3.14.

Useful scripts are in `scripts/`, including gateway install/start/stop helpers and file-work smoke tests. [`KARA_WINDOWS_SMOKE_TESTS.md`](KARA_WINDOWS_SMOKE_TESTS.md) is a manual end-to-end checklist for the Windows-only surface (system inventory, desktop control, scheduled tasks), which the automated suite covers only with mocks.

### Entry points

| Task | Command |
|---|---|
| CLI | `uv run python agent.py` |
| Telegram gateway | `uv run python -m gateway.run` |
| Windows logon task | `uv run python install_gateway.py` (`--uninstall` to remove) |
| Codex device login | `uv run python codex_auth.py login` |
| GitHub device login | `uv run python github_auth.py login` |
| Update + restart gateway | `uv run python update.py` |

`main.py` dispatches the same things as subcommands: `uv run python main.py gateway|update|install|uninstall`, with no argument starting the CLI.

> **Note:** `pyproject.toml` declares `[project.scripts]` (`kara`, `kara-gateway`, `kara-update`, `kara-install`, `kara-codex-auth`, `kara-github-auth`), but the project has no `[build-system]`, so uv treats it as a virtual project and never installs those entry points. `uv run kara-gateway` currently fails with "Failed to spawn". Use the commands above.

## Roadmap

### Gateway auto-start on macOS and Linux

Running Kara 24/7 is the project's main use case, but only Windows can currently register the gateway to start on its own. Linux and macOS users have to launch it in the foreground and restart it by hand after a reboot.

The daemon does not need to change for this. `gateway/run.py` already runs anywhere, and `gateway/restart.py` already spawns replacements and checks process liveness on POSIX. The gap is purely OS service registration:

| Platform | Mechanism | Status |
|---|---|---|
| Windows | Scheduled Task at logon (`install_gateway.ps1`) | done |
| Linux | **systemd user unit** (`systemctl --user enable --now kara-gateway`) | planned |
| macOS | **launchd LaunchAgent** (`~/Library/LaunchAgents/*.plist`) | planned |

A user-level service is the right scope on both — it starts at login, has access to the user's `.env` and `brain/`, and needs no root. Planned work:

- Template a systemd unit and a launchd plist, with the repo path and interpreter filled in at install time rather than committed (the same mistake `launch_gateway.vbs` used to make).
- Make `install_gateway.py` dispatch on `sys.platform` instead of refusing to run, and support `--uninstall` on all three.
- Give `scripts/` POSIX start/stop/restart equivalents, reusing the existing `brain/gateway.pid` and restart-flag files so `/restart` and `kara-update` behave identically everywhere.
- Document log locations per platform: `brain/logs/` stays the source of truth, but systemd also captures stdout in the journal.

Interactive use, the CLI, and every tool group already work on all three platforms today — this is only about keeping the gateway alive unattended.

### Other candidates

- **Console scripts.** `pyproject.toml` declares six `[project.scripts]` entry points that are never installed, because there is no `[build-system]`. Adding a build backend would make them real, but the flat module layout needs explicit packaging config so a built wheel does not omit every top-level module.
- **Dropping the AGPL dependency.** `pymupdf` is the only thing preventing a redistributable bundled artifact. If PDF extraction moved behind an optional extra, the default install would be fully permissive.

## Acknowledgements

Kara's architecture borrows ideas from a few projects, with thanks:

- **[Hermes](https://github.com/nousresearch/hermes-agent)** — the single long-lived gateway process, and the general shape of the desktop-control surface. Kara's implementation differs (it drives `cua-driver`'s one-shot CLI rather than a full backend) and shares no code.
- **[Mnemosyne](https://github.com/mnemosyne-oss/mnemosyne)** — structured memory ideas. It is used here as an optional dependency over MCP, not vendored.

## License

Released under the [MIT License](LICENSE). Copyright © 2026 Odai Ameera.

### Dependency licensing

Kara's own code is MIT, but two runtime dependencies are copyleft and are worth knowing about if you plan to redistribute:

| Dependency | License |
|---|---|
| `pymupdf` | GNU AFFERO GPL 3.0, or an Artifex commercial license |
| `python-telegram-bot` | LGPL-3.0-only |

Everything else is MIT, BSD, or Apache-2.0. These are installed from PyPI on your machine rather than redistributed in this repository, so cloning and running Kara is unaffected. They matter if you ship a *combined* artifact — a wheel with dependencies vendored, a PyInstaller binary, or a container image — or if you host a modified copy as a service, where AGPL's network clause can apply. If you only need the non-PDF features, `pymupdf` is the dependency to drop. This is a pointer, not legal advice.

## Status

This is a personal-agent project under active development, published as-is. Expect APIs, tools, and configuration to evolve. Release notes are in [`CHANGELOG.md`](CHANGELOG.md).
