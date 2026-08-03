# Kara on Buzz via ACP (Linux runtime)

This guide ports Kara's runtime/deployment layer from Windows to Linux without
changing identity provisioning.

## What was ported

The existing Windows service path is:

```text
install_gateway.py
  -> scripts/install_gateway.ps1
  -> Windows Scheduled Task
  -> scripts/launch_gateway.vbs
  -> scripts/run_gateway.py
  -> gateway/run.py
  -> gateway/platforms/telegram.py
```

That implementation is Telegram-only. It does not expose ACP or call Buzz.
Linux Buzz uses a parallel path and leaves every Windows file intact:

```text
systemd --user
  -> buzz-acp (relay subscription, gates, thread context)
  -> .venv/bin/python acp_server.py (ACP JSON-RPC over stdio)
  -> acp_server.py
  -> KaraSession(channel="buzz")
  -> tools/buzz_tools.py
  -> buzz messages send
  -> Buzz relay
```

ACP transports prompts and streamed response events; it does not publish Kara's
final response. `acp_server.py` parses the last valid harness `[Context]` block
before user-controlled thread/message content, binds its channel UUID and reply
destination for the duration of the turn, and rejects prompts without that
trusted route. This supports both threaded prompts (`Thread root`) and new
top-level mentions (the harness's required `--reply-to` destination) without
allowing a later user-supplied context block to redirect the response. The
model-facing `buzz_send_message` accepts content only. A successful tool call
marks the turn published; otherwise the ACP adapter publishes Kara's returned
final text once as a deterministic fallback.

## Credential execution locus

The systemd user service reads `~/.config/kara/buzz.env`. `buzz-acp` needs
`BUZZ_RELAY_URL` and `BUZZ_PRIVATE_KEY`; this relay also requires
`BUZZ_AUTH_TAG`. The harness passes its environment to the Kara ACP Python
process, and Kara's direct `buzz` subprocess. The systemd unit renders a
controlled PATH containing the discovered `buzz` and `buzz-acp` directories.
The tool accepts no credential or routing arguments and launches an argv list
without a shell.

Do not put real values in this repository, command-line arguments, prompts, or
logs. The committed `deploy/buzz.env.example` contains placeholders only.

## Install

Prerequisites:

- Linux with systemd user services, Python 3.14+, and `uv` for installation.
- `buzz-acp` and `buzz` available when the installer runs.
- Kara provider authentication already configured in its gitignored `brain/`
  or `.env` state.
- A separately provisioned agent identity, owner pubkey, relay authorization,
  and channel membership.

### Register Kara as a Buzz custom harness

Buzz Desktop's **Add custom harness** form registers the ACP-speaking child
process, not the outer `buzz-acp` relay harness. Use:

- Name: `Kara` (ID auto-derives to `kara`)
- Command: `<stable-checkout>/.venv/bin/python`
- One argument: `<stable-checkout>/acp_server.py`
- Environment variables: none; Buzz-managed identity variables are injected
  and take precedence

Leave the optional documentation URL and install hint blank unless packaging
Kara for another machine. Select this harness on Kara's agent record. When the
systemd service below is the production supervisor, disable **Start on app
launch** in Desktop; two `buzz-acp` processes using the same identity race each
other and can duplicate or steal turns.

From the repository:

```bash
uv sync
uv run python scripts/install_buzz_acp_linux.py
```

The installer refuses checkouts under a `.scratch` directory. Integrate and
review the work in a stable Kara checkout before creating a production unit;
otherwise systemd would depend on a path Buzz treats as disposable.

The installer renders the stable repository, virtualenv Python, ACP script,
`buzz-acp`, and controlled service PATH into
`~/.config/systemd/user/kara-buzz-acp.service`, creates
`~/.config/kara/buzz.env` from the placeholder example if absent, applies mode
`0600`, and reloads the user manager. It never enables the service before the
operator reviews the environment file.

Required runtime settings:

```dotenv
BUZZ_RELAY_URL=wss://relay.example
BUZZ_PRIVATE_KEY=<dedicated-agent-key>
BUZZ_AUTH_TAG='["auth","owner-pubkey","","signature"]'
BUZZ_ACP_AGENT_OWNER=<64-char-owner-hex-pubkey>
BUZZ_ACP_AGENT_COMMAND=/stable/Kara-Personal-Agent/.venv/bin/python
BUZZ_ACP_AGENT_ARGS=/stable/Kara-Personal-Agent/acp_server.py
BUZZ_ACP_SUBSCRIBE=mentions
BUZZ_ACP_CHANNELS=<comma-separated-channel-uuids>
BUZZ_ACP_RESPOND_TO=owner-only
BUZZ_ACP_ALLOWED_RESPOND_TO=owner-only
BUZZ_ACP_MULTIPLE_EVENT_HANDLING=queue
```

Keep the default mention filter and ignore-self behavior. `owner-only` is the
safe default; identity onboarding owns any later allowlist decision. Kara's
provider calls are synchronous and its ACP cancellation handler cannot stop an
in-flight call, so use `queue` rather than `buzz-acp`'s default `steer` mode;
new mentions are delivered after the current turn instead of being
cancelled-and-reprompted concurrently.

The outer single quotes around `BUZZ_AUTH_TAG` are required for systemd's
`EnvironmentFile` parser to preserve the JSON double quotes. Validate the file
without printing any secret value before starting the service:

```bash
uv run python scripts/install_buzz_acp_linux.py --validate-config
```

## Start and health check

```bash
systemctl --user enable --now kara-buzz-acp.service
systemctl --user is-active kara-buzz-acp.service
systemctl --user status kara-buzz-acp.service --no-pager
journalctl --user -u kara-buzz-acp.service -n 100 --no-pager
```

Do not inspect `systemctl show ... Environment` or `/proc/<pid>/environ`.
Presence-only checking inside Kara is safe:

```bash
for name in BUZZ_RELAY_URL BUZZ_PRIVATE_KEY BUZZ_AUTH_TAG; do
  if printenv "$name" >/dev/null; then printf '%s=present\n' "$name"; else printf '%s=absent\n' "$name"; fi
done
```

Validate the ACP handshake before enabling relay traffic:

```bash
buzz-acp models \
  --agent-command "$PWD/.venv/bin/python" \
  --agent-args "$PWD/acp_server.py" \
  --json
```

## Threaded reply validation

1. Mention Kara from the configured owner in an enrolled test channel.
2. Ask for a short reply in the same thread.
3. Confirm the turn produces exactly one accepted event with a non-empty event
   ID, whether Kara used `buzz_send_message` or the adapter fallback.
4. Confirm the event's reply marker points to the supplied Buzz reply
   destination and the UI renders it under the triggering thread.

For a non-secret direct transport probe from the same environment, bind the
same route shape the ACP boundary would bind:

```bash
uv run python -c 'from tools.buzz_tools import set_buzz_request_context,buzz_send_message; set_buzz_request_context(channel="<channel>",reply_to="<reply-event>"); print(buzz_send_message("Kara Linux runtime probe"))'
```

This sends a real message. Use only an existing managed test identity and an
authorized channel; never use placeholder credentials.

On 2026-08-03, the pre-hardening `tools.buzz_tools` path published an accepted
threaded probe from this Linux host with event ID
`8d22dcc2f2ddcd3a56c0bc3165d3bd9d7db3eeebdf1a6329623561b932ddcb72`.
No credential value was inspected. That probe validated outbound transport
only, not the full inbound ACP turn; repeat the owner mention test after service
installation for end-to-end evidence.

## Restart and rollback

After configuration or code changes:

```bash
systemctl --user restart kara-buzz-acp.service
systemctl --user is-active kara-buzz-acp.service
journalctl --user -u kara-buzz-acp.service -n 100 --no-pager
```

Rollback:

1. Stop the service before switching code: `systemctl --user stop kara-buzz-acp.service`.
2. Restore the last reviewed commit/configuration.
3. Re-run the installer so absolute paths match the restored checkout.
4. Start the service and repeat the ACP handshake and threaded-reply test.

To remove the Linux unit while preserving credentials for recovery:

```bash
uv run python scripts/install_buzz_acp_linux.py --uninstall
```

Never run two harnesses for the same relay/pubkey identity. If credentials may
have leaked, service rollback is insufficient; use the identity-track
rotation/revocation procedure.

## Validation sources

- Windows files inspected: `install_gateway.py`,
  `scripts/install_gateway.ps1`, `scripts/launch_gateway.vbs`,
  `scripts/run_gateway.py`, `gateway/run.py`, and
  `gateway/platforms/telegram.py`.
- Linux implementation: `acp_server.py`, `tools/buzz_tools.py`,
  `scripts/install_buzz_acp_linux.py`, `deploy/systemd/kara-buzz-acp.service`,
  and `deploy/buzz.env.example`.
- Live host interfaces checked 2026-08-03: `buzz-acp --help`,
  `buzz-acp models --help`, and `buzz --help`.
