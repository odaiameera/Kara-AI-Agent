# Kara — Local Personal AI Agent

Kara is a local-first personal AI agent for CLI and Telegram. She combines configurable chat providers with persistent memory, local PC/file tools, scheduling, email, GitHub, and safety-gated desktop automation.

> **Privacy and safety:** Kara’s runtime state, credentials, provider tokens, local memory, and `.env` are intentionally machine-local and gitignored. Read-only inspection is the default; publishing, test execution, and desktop input require explicit approval.

## What Kara can do

- Chat through configurable providers, including Ollama and OpenAI Codex OAuth.
- Run continuously through a Telegram gateway or Buzz ACP harness with SQLite-backed conversation history.
- Maintain a local brain: always-in-context core memory, durable learnings, session logs, and hybrid semantic/keyword recall.
- Use optional Mnemosyne MCP memory as an additional structured memory surface.
- Search and fetch the web using SearXNG with Brave/DuckDuckGo fallbacks.
- Read, search, and explicitly write local files inside configured filesystem roots.
- Create/read/edit Word, Excel, and PowerPoint files without opening Microsoft Office.
- Extract text from PDFs and images locally using PyMuPDF and Windows OCR.
- Inspect SQLite databases and Python source safely; run a bounded unittest suite only after approval.
- Read email through Himalaya; sending is separately disabled by default.
- Inspect Windows system/process/service/task/disk state without modifying it.
- Inspect or control desktop apps through `cua-driver`, with exact two-turn approval for input.
- Create durable reminders and scheduled autonomous read-only jobs.
- Read GitHub repositories, commits, issues, PRs, Actions, and notifications; publishing actions require approval.

## Architecture

```text
CLI / Telegram / Buzz
     │
     ▼
KaraSession ── provider chat + tool loop ── tool registry
     │                                      │
     ├── SQLite conversation state           ├── local files/documents/OCR
     ├── local brain + vector index          ├── web/email/GitHub/MCP
     ├── scheduler                           └── Windows/computer inspection
     └── optional Mnemosyne MCP
```

`kara.py` owns the main model → tool → model loop. Tool schemas are generated from Python function signatures and docstrings. Telegram handlers run blocking provider, SQLite, and tool work in worker threads so the event loop remains responsive.

## Local brain

Everything private and persistent lives in `brain/` and is gitignored:

```text
brain/
  core/          Always-in-context persona, user, and active-task blocks
  learnings/     Durable facts and decisions (Markdown)
  sessions/      Episodic conversation logs (Markdown)
  index/         Derived vector index for hybrid search
  settings.json  Active provider/model settings
  providers.json Provider definitions without API keys
  state.db       SQLite conversation history
  scheduler.db   Durable reminders and scheduled jobs
  auth.json      OAuth tokens (GitHub/Codex), when configured
  logs/          Gateway logs
```

Kara’s built-in semantic search uses cached hybrid ranking: embeddings plus keyword matching. A cheap stat fingerprint avoids re-reading/re-embedding memory files when they have not changed. If embeddings are unavailable, search falls back to keywords.

## Requirements

- Python **3.14+**
- [uv](https://docs.astral.sh/uv/)
- An Ollama setup or another configured chat provider
- Windows 10/11 for Windows inventory, Windows OCR, and `cua-driver` desktop support

## Setup

```powershell
cd personal_agent
uv sync
Copy-Item .env.example .env
```

Add at least one provider to `.env`, for example:

```env
OLLAMA_API_KEY=your_ollama_api_key
OLLAMA_MODEL=gpt-oss:120b
```

Run the CLI:

```powershell
uv run agent.py
```

For local Ollama, leave `OLLAMA_API_KEY` blank and run an Ollama server locally (default `http://localhost:11434`). Install any required local chat and embedding models separately.

## Providers and models

Kara uses a small provider abstraction, so provider-specific authentication does not leak into the agent loop. Ollama supports cloud or local operation; OpenAI Codex OAuth is also supported when configured.

Telegram/CLI commands include:

- `/providers` — list configured providers
- `/provider <id> [model]` — switch provider, optionally with a model
- `/models` — list models across providers
- `/model [provider/model-or-name]` — inspect or switch model
- `/new` — start a fresh chat while preserving long-term memory
- `/restart` — request a graceful gateway restart

Switching provider or model resets only the current chat context, not the brain.

## 24/7 Telegram gateway

Install the Windows logon task:

```powershell
uv run install_gateway.py
```

This creates the `KaraGateway` Scheduled Task and launches the gateway without a console window. For debugging:

```powershell
uv run gateway.py
```

The gateway auto-restarts after source changes, persists conversations in `brain/state.db`, and delivers pending scheduler results after restarts. Regular chat replies render Markdown as Telegram HTML; malformed formatting falls back to plain text. Commands intentionally remain plain text.

## Buzz on Linux (ACP)

Kara exposes `acp_server.py`, a newline JSON-RPC ACP server for the `buzz-acp`
harness. The ACP boundary binds the harness-supplied channel and thread before
Kara runs. `buzz_send_message` accepts content only, and the adapter publishes
Kara's final response as a fallback when the model does not call the tool.
Credentials and routing IDs are never model tool arguments.

Install the owner-only systemd user-service template:

```bash
uv run python scripts/install_buzz_acp_linux.py
```

Review `~/.config/kara/buzz.env`, keep it mode `0600`, then enable the service:

```bash
systemctl --user enable --now kara-buzz-acp.service
```

See [`docs/BUZZ_ACP_LINUX.md`](docs/BUZZ_ACP_LINUX.md) for configuration,
health checks, threaded-reply validation, restart, and rollback. Identity and
relay/channel enrollment are intentionally documented separately.

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

### Windows inspection and desktop use

Read-only Windows inventory tools:

- `system_overview`
- `list_processes`
- `list_services`
- `list_scheduled_tasks`
- `disk_usage`

They use fixed PowerShell inventory scripts and cannot stop processes, change services, edit tasks, or alter disks.

`computer_use` uses the installed `cua-driver` to list apps/windows and capture accessibility elements. Clicks, typing, keys, scrolling, focus, and foregrounding require a fresh approval token bound to the exact target process/window/title. Keyboard actions verify the approved target is foreground before input is delivered; if Windows refuses activation, the action fails closed rather than acting on another window.

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
