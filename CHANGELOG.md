# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is `0`, the configuration surface and tool APIs may
still change between minor releases.

## [0.2.0] - 2026-08-14

The first release prepared for public use. `0.1.0` was the initial MVP and was
never tagged, so everything below has accumulated since then.

### Changed defaults

These change behavior for an existing install. Each can be restored in `.env`.

- **Web search no longer defaults to a private SearXNG instance.** `SEARXNG_URL`
  now ships empty, and `web_search` falls through to the public providers
  (Brave, then DuckDuckGo) it already supported. Previously every unconfigured
  install sent its search queries to one specific third-party server. Set
  `SEARXNG_URL` to point at your own instance again.
- **`KARA_TIMEZONE` defaults to `UTC`** instead of a hardcoded local zone.
  Reminders and cron schedules resolve in UTC unless you set your own zone.
- **Kara refers to "the user"** rather than a hardcoded name. Set
  `KARA_USER_NAME` to have Kara address you by name; this seeds core memory and
  the persona on first run.

### Added

- Support for **any OpenAI-compatible provider** from `.env` alone — no code
  changes. Set `KARA_PROVIDER_<NAME>_BASE_URL` (plus optional `_API_KEY` and
  `_MODEL`) and it is discovered as a provider. Works with OpenAI, Groq,
  Together, OpenRouter, DeepSeek, Mistral, Fireworks, vLLM, and LM Studio.
- An **OpenAI Codex provider** backed by a device-code OAuth flow.
- **On-demand tool groups.** A session starts with the always-on groups (memory,
  web, file, document) and reveals the rest from message keywords or an explicit
  `activate_tool_group` call, instead of sending all 84 tool schemas on every
  request (~40KB down to ~8.8KB for a typical request).
- **Turn bounds.** `KARA_MAX_TOOL_ITERATIONS`, `KARA_TURN_TIMEOUT_SECONDS`, and
  `KARA_MAX_REPEATED_TOOL_CALLS` stop a runaway tool loop, and a running turn
  can be interrupted with `/stop`.
- **Context compaction.** Older conversation is trimmed and summarized once it
  would exceed `KARA_COMPACT_AT_FRACTION` of the window, so a long-running chat
  does not eventually stop working. `/context` reports current usage.
- **Provider resilience.** Transient failures (429, 5xx, dropped connections)
  are retried with backoff and a failed turn is rolled back rather than leaving
  partial state.
- **Per-turn observability.** A `turns` table records tokens, tool calls,
  duration, and iteration count; `/usage` reports it for the session and today.
  Tool failures are marked explicitly rather than inferred from prose.
- **Concurrent read-only tool batches**, which also stopped the tool loop from
  busting the provider's prompt cache.
- **Durable reminders and autonomous scheduled jobs**, surviving gateway and
  machine restarts, restricted to a read-only tool allowlist when run unattended.
- **Cross-platform desktop control** through `cua-driver` on Windows, Linux, and
  macOS, with two-turn approval bound to an exact process/window/title.
- A **hidden live clock** injected into every request, quantized to the minute so
  it does not defeat prompt caching.
- `KARA_USER_NAME` for configuring what Kara calls you.

### Changed

- Conversation transcripts moved to **SQLite** (`brain/state.db`); only curated
  summaries and saved learnings are embedded for semantic recall, so the index
  no longer grows with every scheduled run.
- Provider responses are normalized into a single **`ChatResult`** type, so
  nothing above the provider layer knows which backend answered.
- Each `tools/` module now **declares its own** `TOOL_GROUP`, `TOOLS`,
  `SCHEDULED_SAFE`, and `READ_ONLY`, aggregated by `tools/registry.py` — adding
  a tool means editing one file.
- The tool-group vocabulary is **derived rather than restated**. `ALWAYS_ON` and
  `GROUP_KEYWORDS` are validated against the declared groups at import, and the
  `activate_tool_group` schema the model reads is generated. Previously all four
  copies could drift apart silently, quietly disabling a capability.
- Ollama is asked for an **explicit context window**; its 4096 default silently
  truncated prompts larger than Kara's own system prompt and tool schemas.
- Tool results are bounded **per batch**, not only per individual result.

### Fixed

- **Paths derived from `REPO_ROOT` escaped the checkout.** It was the parent of
  the package directory, correct when the code lived in a `personal_agent/`
  subdirectory but not after the layout was flattened. The identity seed and the
  legacy-memory migration read from a sibling of the repository, and
  `kara-update` probed `../.git` and would have run `git pull` one directory up.
  Nothing failed loudly, because the missing seed simply fell back to the
  built-in persona.
- **The system prompt hardcoded a scheduling timezone** while the runtime clock
  read `KARA_TIMEZONE`. Anyone who configured a zone got two contradictory
  answers in the same request, and reminders could land at the wrong hour.
- **The Windows gateway launcher contained absolute paths** from one machine.
  `scripts/launch_gateway.vbs` was a generated file whose output was committed,
  so it only worked on the machine that generated it. It now resolves its own
  location and needs no regeneration after moving the repository.
- The `kara-gateway` console script no longer collides with the `gateway/`
  package; duplicate launchers were removed.
- GitHub pushes are non-interactive and bounded, so a credential prompt cannot
  hang the agent.
- Error messages and comments referenced a `personal_agent/` directory that
  stopped existing when the layout was flattened.

### Security

- Git subprocesses run with an **isolated environment**, and GitHub credentials
  are kept out of child processes; Git credential help is served only for HTTPS
  requests to github.com.
- Approval tokens are single-use, expire after 10 minutes, and are bound to the
  originating session, the exact action and arguments, and the resolved target
  window. The gate compares the literal user message, so the model cannot
  approve an action by emitting the phrase itself.
- Sensitive paths (`.env`, credential stores, application profiles) remain
  blocked to the file tools unless `KARA_ALLOW_SENSITIVE_FILES` is set.

### Documentation

- Released under the **MIT License** — the project previously shipped no
  `LICENSE` file, so default copyright applied and no reuse rights were granted.
  Added an acknowledgements section crediting the projects Kara borrows
  architecture from, and a note on the two copyleft runtime dependencies.
- Fixed the outbound HTTP `User-Agent`, which advertised another project's
  repository URL, so `web_search` and `web_fetch` identified themselves as
  Hermes to every server they contacted.
- Audited the README against the source and corrected what had drifted: the
  scheduled-job allowlist, the available slash commands, the `brain/` layout, and
  the description of web search.
- Documented subsystems that had no coverage at all: the Obsidian tool group,
  the five memory tools, the console scripts installed by `uv sync`, and a repo
  module map.
- Added a "What this is for" section and expanded the configuration table.
- Replaced machine-specific example paths and personal identifiers throughout.

### Known issues

- Four `tests/test_mcp_bridge.py::McpBridgeRealServerTests` cases fail under
  CPython **3.14.0rc2** with `_eval_type() got an unexpected keyword argument
  'prefer_fwd_module'`, an incompatibility between the MCP SDK's pydantic use and
  that release candidate. They pass on a release build of 3.14 and are unrelated
  to Kara's own code.
- The `[project.scripts]` entry points (`kara`, `kara-gateway`, `kara-update`,
  `kara-install`, `kara-codex-auth`, `kara-github-auth`) are declared but never
  installed: `pyproject.toml` has no `[build-system]`, so uv treats the project
  as virtual and builds nothing. `uv run kara-gateway` fails with "Failed to
  spawn". Use `uv run python -m gateway.run` and the other forms listed in the
  README. Adding a build backend would fix this, but the flat module layout
  needs explicit packaging config to avoid shipping a broken wheel.
- `pymupdf` is AGPL-3.0 (or commercial) and `python-telegram-bot` is LGPL-3.0.
  Both are installed from PyPI rather than redistributed here, so this does not
  affect cloning and running Kara, but it constrains shipping a bundled artifact
  or hosting a modified copy as a service.

## [0.1.0] - 2026-06-28

Initial MVP: CLI chat against Ollama with a file-backed local brain (core
memory, learnings, session logs) and the first tool surface. Never tagged; this
entry exists to mark the starting point for the history above.

[0.2.0]: https://github.com/odaiameera/Kara-Agent-WorkInProgress/releases/tag/v0.2.0
[0.1.0]: https://github.com/odaiameera/Kara-Agent-WorkInProgress/commits/main
