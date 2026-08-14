# Kara — Local Personal AI Agent

Kara is a local-first personal AI agent for CLI and Telegram. They combine configurable chat providers with persistent memory, local PC/file tools, scheduling, email, GitHub, and safety-gated desktop automation.

> **Privacy and safety:** Kara’s runtime state, credentials, provider tokens, local memory, and `.env` are intentionally machine-local and gitignored. Read-only inspection is the default; publishing, test execution, and desktop input require explicit approval.

## What Kara can do

- Chat through configurable providers: Ollama, OpenAI Codex OAuth, and any OpenAI-compatible endpoint (OpenAI, Groq, OpenRouter, DeepSeek, vLLM, LM Studio) configured from `.env` alone.
- Run continuously through a Telegram gateway with SQLite-backed conversation history.
- Maintain a local brain: always-in-context core memory, durable learnings, session logs, and hybrid semantic/keyword recall.
- Use optional Mnemosyne MCP memory as an additional structured memory surface.
- Search and fetch the web using SearXNG with Brave/DuckDuckGo fallbacks.
- Read, search, and explicitly write local files inside configured filesystem roots.
- Create/read/edit Word, Excel, and PowerPoint files without opening Microsoft Office.
- Extract text from PDFs and images locally using PyMuPDF plus Windows OCR or Tesseract.
- Inspect SQLite databases and Python source safely; run a bounded unittest suite only after approval.
- Read email through Himalaya; sending is separately disabled by default.
- Inspect Windows system/process/service/task/disk state without modifying it.
- Inspect or control desktop apps through `cua-driver`, with exact two-turn approval for input.
- Create durable reminders and scheduled autonomous read-only jobs.
- Read GitHub repositories, commits, issues, PRs, Actions, and notifications; publishing actions require approval.

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
  index/         Derived vector index for hybrid search
  settings.json  Active provider/model settings
  providers.json Provider definitions without API keys
  state.db       SQLite conversation history and session summaries
  scheduler.db   Durable reminders and scheduled jobs
  auth.json      OAuth tokens (GitHub/Codex), when configured
  logs/          Gateway logs
```

Conversation transcripts live in one place: the `messages` table in `state.db`. Semantic recall is built from two curated sources instead — `learnings/` (facts Kara chose to save) and the `session_summaries` table (one recap per finished conversation). Raw turns are deliberately not embedded, so recall returns decisions rather than small talk, and the index no longer grows with every scheduled job that runs.

Existing `brain/sessions/*.md` logs are migrated once on startup: any `## Summary` block is lifted into `session_summaries` and the files are left on disk untouched.

Kara’s built-in semantic search uses cached hybrid ranking: embeddings plus keyword matching. A cheap fingerprint — file stat for learnings, row count and latest id for summaries — avoids re-embedding when nothing has changed. If embeddings are unavailable, search falls back to keywords.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python **3.14+** (`uv` can install and manage it)
- One chat provider: local Ollama, Ollama Cloud, or OpenAI Codex OAuth
- Optional: Windows 10/11, a supported Linux desktop, or macOS for `cua-driver` desktop support
- Optional image OCR: built-in Windows OCR, or Tesseract (`tesseract-ocr` on Linux; `brew install tesseract` on macOS)

## Quick start: clone and run Kara

The repository is currently private, so cloning requires a GitHub account with access. The same commands will work without authentication if the repository is made public later.

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
uv run kara-gateway
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

Switching provider or model resets only the current chat context, not the brain.

## 24/7 Telegram gateway on Windows

Install the Windows logon task:

```powershell
uv run install_gateway.py
```

This creates the `KaraGateway` Scheduled Task and launches the gateway without a console window. For debugging:

```powershell
uv run kara-gateway
```

The gateway auto-restarts after source changes, persists conversations in `brain/state.db`, and delivers pending scheduler results after restarts. Regular chat replies render Markdown as Telegram HTML; malformed formatting falls back to plain text. Commands intentionally remain plain text.

## Configuration

Copy `.env.example` and set only the integrations you use. Never commit `.env`.

| Area | Key settings |
|---|---|
| Providers | `OLLAMA_API_KEY`, `OLLAMA_HOST`, `OLLAMA_MODEL`, `EMBED_MODEL` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` |
| Web search | `SEARXNG_URL`, Cloudflare Access variables, public fallback URLs |
| Files | `KARA_FILE_READ_ROOTS`, `KARA_FILE_WRITE_ROOTS`, `KARA_ALLOW_SENSITIVE_FILES` |
| Documents | `KARA_DOCUMENT_MAX_BYTES`, `KARA_PDF_MAX_PAGES`, OCR/PDF time and memory limits |
| Desktop | `KARA_CUA_ENABLED`, `KARA_CUA_DRIVER_CMD`, `KARA_CUA_TELEMETRY` |
| Scheduling | `KARA_TIMEZONE`, `KARA_SCHEDULER_POLL_SECONDS` |
| GitHub | `GITHUB_CLIENT_ID`, `GITHUB_OAUTH_SCOPES`, `GITHUB_GIT_TIMEOUT` |
| Mnemosyne | `MNEMOSYNE_BIN`, `MNEMOSYNE_DB_PATH` |
| Email | Himalaya configuration plus `EMAIL_SEND_ENABLED=true` only when deliberate |

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

Autonomous scheduled agent jobs are deliberately restricted to observational tools: web, memory search, file/document/SQLite/Python reads, and Windows inventory. They cannot write files, send email, drive the desktop, execute tests, mutate memory, or create jobs.

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

## Security model

- `.env`, `brain/`, virtual environments, and downloaded binaries are gitignored.
- Credentials are not passed to third-party subprocesses unless that subprocess requires its own explicitly scoped configuration.
- Local reads/writes are rooted and sensitive paths are blocked by default.
- Web content and downloaded text are never treated as permission to change local files.
- SQLite and Windows inventory are read-only.
- Email sending, test execution, desktop input, and GitHub publishing use separate safety gates.
- Approval tokens are one-time, short-lived, user-authored, and bound to the exact proposed action/target where applicable.

## Development

The repository includes an extensive `unittest` suite for tools, auth, providers, scheduling, document artifacts, gateway behavior, and safety boundaries. Run it only in a trusted local development environment:

```powershell
uv run python -m unittest discover -s tests
```

Useful scripts are in `scripts/`, including gateway install/start/stop helpers and file-work smoke tests.

## License / status

This is a private personal-agent project under active development. Expect APIs, tools, and configuration to evolve.
