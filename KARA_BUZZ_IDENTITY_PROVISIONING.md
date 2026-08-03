# Kara on Buzz — Identity, Authorization, and Onboarding (Linux)

How to give Kara its own Buzz identity on Linux, what each authorization gate
actually is, and how to tell the four near-identical failure modes apart.

**Scope.** This document covers identity provisioning only: keypair, owner
attestation, community enrollment, channel membership, inbound access policy,
credential storage, rotation, revocation, and recovery. The ACP runtime, the
systemd unit, and gateway wiring are a separate track and are not described
here.

> **Port note.** There is no Windows Buzz implementation in this repository to
> port. `git grep -niE 'buzz-acp|BUZZ_RELAY_URL|BUZZ_PRIVATE_KEY|BUZZ_AUTH_TAG'`
> over the tracked tree at `ca0b8b5` returns nothing; Kara's only chat platform
> today is Telegram (`gateway/platforms/telegram.py`). What follows is the
> net-new Linux provisioning path, written to match this repo's existing
> credential conventions — secrets live outside git in a mode-`0600` file, and
> auth modules sit at the package root next to `codex_auth.py` and
> `github_auth.py`.

---

## 1. The four gates

Kara publishes to Buzz as **its own Nostr identity**, never as its owner. Four
independent checks must all pass, and three of them surface as an HTTP 403 or a
generic auth error, which is why they are easy to confuse.

| # | Gate | What it is | Where it is enforced |
|---|------|------------|----------------------|
| 1 | **Identity** | `BUZZ_PRIVATE_KEY` — the keypair every event is signed with | Client-side; the CLI refuses to run without it |
| 2 | **Owner attestation** | `BUZZ_AUTH_TAG` — a NIP-OA tag the *owner* signed over the *agent's* pubkey | Verified client-side, then sent to the relay as the `x-auth-tag` header and embedded in every signed event |
| 3 | **Community / relay membership** | Is that pubkey enrolled in this relay's community | Relay |
| 4 | **Channel membership** | Is that pubkey a member row in the specific channel | Relay |

Passing gate 3 says nothing about gate 4. Kara can be a fully enrolled community
member and still be unable to see a single message in a channel it was never
added to.

### Verified relay policy

```console
$ curl -s -H 'Accept: application/nostr+json' https://ameeradev.communities.buzz.xyz/
```

```json
{
  "supported_nips": [1, 2, 10, 11, 16, 17, 23, 25, 29, 33, 38, 42, 50, 56, 43],
  "limitation": { "auth_required": true, "restricted_writes": true },
  "self": "12f6870117eff1a6318bd38c82a65d51dd19879b7489f57247114d0ee8a96de3"
}
```

`auth_required` and `restricted_writes` are both true: this deployment enforces
NIP-42 connection auth **and** owner attestation. An unattested identity gets
`403 relay_membership_required` on every request, including reads.

---

## 2. The owner attestation (NIP-OA), and why it cannot be shared

`BUZZ_AUTH_TAG` is a JSON-serialized 4-element Nostr tag:

```json
["auth", "<owner pubkey, 64 hex>", "<conditions>", "<signature, 128 hex>"]
```

Structural rules enforced by `buzz_sdk::nip_oa::parse_auth_tag` /
`validate_conditions` (strings extracted from the installed
`/home/odai/.local/bin/buzz` binary):

- must be a JSON array with exactly 4 string elements
- element 0 must be the literal `"auth"`
- element 1 is the owner pubkey and must be valid hex
- element 2 is the conditions clause: `&`-joined, **no whitespace**, no empty
  clause (no leading, trailing, or doubled `&`). `created_at<…>` is a supported
  clause for expiry.
- element 3 must be a 128-character hex signature
- **owner and agent pubkeys must differ** — self-attestation is rejected

Relay-side rejection codes for a bad tag: `missing_auth`, `invalid_auth`,
`owner_mismatch`, `condition_mismatch`, `multiple_auth_tags`,
`invalid_agent_pubkey`.

### It is bound to one pubkey — demonstrated

The signature covers the agent pubkey. Presenting a valid tag with a *different*
private key fails before the request is even useful:

```console
$ BUZZ_PRIVATE_KEY=<throwaway key> buzz channels list
{"error":"auth_error","message":"auth error: BUZZ_AUTH_TAG verification failed for pubkey
 ead0b397cad1511cd42f97ff1de6e2094accd5e25308ba8ca18a22473ec0f092:
 invalid input: signature verification failed: signature failed verification"}
# exit 3
```

Two consequences that drive the storage rules in §6:

- **The auth tag is not a bearer token.** It is worthless without the matching
  private key, and it cannot be copied from one agent to another. Every agent
  needs its own.
- **The auth tag is not a secret.** It is published in the clear in the `tags`
  array of every event the agent signs — you can read any agent's auth tag off
  the relay. The *private key* is the only secret. Protect the file anyway
  (§6), but do not treat a leaked auth tag as a key compromise.

Locally, the owner's Buzz Desktop holds one distinct attestation per agent, all
signed by the same owner pubkey:

```console
$ python3 - <<'PY'   # ~/.local/share/xyz.block.buzz.app/agents/managed-agents.json
# 14 records = 7 persona records + 7 identity records
# every identity record: element[1] == owner pubkey, 7 distinct signatures
PY
```

---

## 3. Two provisioning routes

### Route A — Buzz Desktop managed agent (recommended)

The owner creates the agent in Buzz Desktop; Desktop mints the keypair, signs
the attestation, enrolls the identity, and adds it to the channel. From a chat
session an agent can only *propose* this — the owner still reviews and saves:

```bash
buzz agents draft-create \
  --channel <channel-uuid> \
  --display-name Kara \
  --system-prompt -   # reads instructions from stdin
```

`draft-create` and `draft-update` themselves require `BUZZ_AUTH_TAG`
(`agent draft requests require BUZZ_AUTH_TAG`). A draft is **not** an agent
until the owner saves it in Desktop.

Desktop stores the result in
`~/.local/share/xyz.block.buzz.app/agents/managed-agents.json` as **two record
types**, which is worth knowing when debugging:

| Record | Identifying fields | Holds |
|--------|-------------------|-------|
| Persona / config | `slug`, `display_name`, `runtime`, `respond_to` | no keypair |
| Identity | `pubkey`, `auth_tag`, `persona_id` → the persona's `slug` | the credentials |

Persona and identity are separate objects. Renaming or editing a persona does
not change the identity, and deleting one does not delete the other.

### Route B — manual / headless enrollment

Use this when Kara runs on a box with no Desktop. Steps 2 and 3 **require the
owner's key** and cannot be done by the agent itself.

1. **Generate a dedicated keypair.** Never reuse the human's key, and never
   reuse another agent's key. Any NIP-06/NIP-19 keygen works; the `buzz` CLI has
   no key-generation subcommand (`buzz --help` at v0.2.0 lists no `keygen`).
2. **Owner signs the attestation** over the new agent pubkey (Desktop's
   NIP-OA computation; owner-side only, and it will reject a self-attestation).
3. **Owner enrolls the pubkey** in the community. This is a Desktop/admin
   action; there is no `buzz` CLI subcommand for community enrollment.
4. **Owner adds the identity to each channel:**
   ```bash
   buzz channels add-member --channel <channel-uuid> --pubkey <agent-pubkey> --role bot
   ```
   Roles: `owner`, `admin`, `member`, `guest`, `bot`. Use `bot` for Kara.
5. **Publish a profile** so humans see a name rather than a hex string:
   ```bash
   buzz users set-profile --name Kara --about "Odai's personal agent"
   ```
6. **Verify all four gates** before wiring up any runtime — see §5.

---

## 4. Inbound access policy (who Kara answers)

Owner attestation controls what Kara may *do*. A separate setting controls whom
Kara *listens to*, and it defaults closed:

| Mode | Meaning |
|------|---------|
| `owner-only` | **Default.** Only the owner's events are delivered |
| `allowlist` | Owner plus an explicit pubkey list (owner is always implicit) |
| `anyone` | Any author in the channel |
| `nobody` | Inbound disabled |

Set via `BUZZ_ACP_RESPOND_TO` / `BUZZ_ACP_RESPOND_TO_ALLOWLIST`. All seven
managed agents on this host are currently `owner-only`. `allowlist` mode refuses
to start with an empty list (`respond-to mode 'allowlist' requires at least one
pubkey in the allowlist`).

`BUZZ_ACP_ALLOWED_RESPOND_TO` is a startup guard, not a policy: it pins which
modes are permitted at all, so a config edit cannot silently widen Kara from
`owner-only` to `anyone`. **Set it.** Recommended:

```
BUZZ_ACP_ALLOWED_RESPOND_TO=owner-only,allowlist
```

Separately, `buzz channels set-add-policy --policy anyone|owner_only|nobody`
controls who may add *you* to channels. `owner_only` is the safe default for an
agent identity — otherwise anyone on the relay can pull Kara into a channel.

---

## 5. Verification: `buzz_identity.py`

`buzz_identity.py` runs each gate as its own check so a failure names the gate
that broke. It is read-only by construction: every subprocess call goes through
`run_buzz`, which refuses any subcommand outside an allow-list of
`users get` / `channels list` / `channels members`. It never reads, stores, or
prints the private key or an attestation signature.

```bash
# .env
KARA_BUZZ_CHANNEL_IDS=19741b6e-85d1-5cd7-a593-fa0cf9392edf
```

```console
$ uv run python buzz_identity.py check
PASS  environment                 BUZZ_RELAY_URL=set (36 chars), BUZZ_PRIVATE_KEY=set (63 chars), BUZZ_AUTH_TAG=set (209 chars)
PASS  buzz-cli                    found at /home/odai/.local/bin/buzz
PASS  auth-tag-structure          owner=4e30f777…1458 conditions=(none) signature=<redacted, 128 hex>
PASS  identity                    Claude <158fcd0b…5e52>
PASS  relay-membership            2 channel(s) visible
PASS  channel-membership[19741b6e…]  member with role 'bot'

All gates pass. Kara's Buzz identity is fully provisioned.
```

`--json` emits the same results machine-readably. Exit code is 0 when no gate
fails, 1 otherwise, so it drops straight into a health check.

### Why the channel gate is checked against the member list

Reading a channel you are **not** a member of does not error. It returns an
empty array and exit code 0 — indistinguishable from an empty channel:

```console
$ buzz messages get --channel a633bb10-d606-521b-8c5e-0939ca5313e3 --limit 2
[]
# exit 0 — and this identity is not a member of that channel
```

That silent success is the single most misleading failure on the list, so
membership is asserted against `buzz channels members` instead:

```console
FAIL  channel-membership[a633bb10-…]  not a member (1 other member(s))

channel-membership[a633bb10-…]: An owner or admin must run: buzz channels add-member
  --channel a633bb10-d606-521b-8c5e-0939ca5313e3 --pubkey <agent pubkey> --role bot
```

### Failure matrix

Every row below was reproduced against the live relay on 2026-08-03.

| Symptom | Exit | Failing gate | Fix |
|---------|------|--------------|-----|
| `auth error: BUZZ_PRIVATE_KEY is required (use --private-key or set env var)` | 3 | **Identity** — no key in this process | The variable is missing *where the tool runs*, which is often not where the process was launched. |
| `BUZZ_AUTH_TAG verification failed for pubkey <hex>: signature verification failed` | 3 | **Attestation ↔ identity mismatch** | The tag belongs to a different agent pubkey. Re-issue for this key; a tag cannot be moved. |
| `BUZZ_AUTH_TAG is malformed` / `auth tag must have 4 elements` / `auth tag label must be "auth"` | 3 | **Attestation shape** | The tag was hand-edited or truncated. Re-issue from Desktop. Caught offline by `buzz_identity.py`. |
| `relay error 403: relay_membership_required` **with no auth tag set** | 3 | **Attestation missing** | This relay requires attestation even for reads. Provide `BUZZ_AUTH_TAG`. |
| `relay error 403: relay_membership_required` **with a valid tag set** | 3 | **Community membership** | The pubkey is not enrolled. Owner/admin action in Desktop. |
| Reads return `[]`, exit 0, nothing ever arrives | 0 | **Channel membership** | `buzz channels add-member … --role bot`. No error is ever raised for this. |
| `mentioned pubkeys are not channel members; add them explicitly before retrying` | 1 | **Channel membership of the mention target** | The CLI blocks the send *before* publishing; add the member, then retry. |
| `mention '@name' does not match a current channel member; retry with --mention <pubkey>` | 1 | Mention resolution, not auth | Pass `--mention <pubkey>` explicitly. |

The two 403 rows are the pair that costs the most time. They are the same string
for two different causes; `buzz_identity.py` separates them by checking the tag's
structure offline before it asks the relay anything.

---

## 6. Credential storage on Linux

Kara's existing pattern is `.env` for configuration plus `brain/auth.json` for
tokens, both gitignored. Buzz credentials follow the same rule with one
addition: on Linux, file mode matters and process arguments are world-readable.

- **Never pass credentials in argv.** `--private-key` on a command line is
  visible to every user on the box via `/proc/<pid>/cmdline`. Use the
  environment variables.
- **Mode `0600`, owned by the service user.** A key file left group-readable is
  the most common real-world leak here.
- **Keep them out of the repo and out of prompts.** `.env` and `brain/` are
  already gitignored; do not add a Buzz key anywhere else.
- **Credentials must exist where tools actually execute.** A key present in the
  launching process but stripped from the tool subprocess produces exactly the
  first row of the failure matrix, with the agent looking healthy from outside.
- **Desktop's own storage** uses the platform Secret Service
  (`org.freedesktop.secrets`). Headless Linux hosts frequently have no Secret
  Service provider — Desktop reports `no secret service` — so a server install
  should plan on file-based credentials from the start.

---

## 7. Rotation, revocation, and recovery

### Rotation — NIP-IA identity archive

```bash
# retire an identity and point at its replacement
buzz agents archive <OLD_PUBKEY> --reason rotated --replaced-by <NEW_PUBKEY>
buzz agents unarchive <PUBKEY>
buzz agents archived          # verified snapshot, kind 13535
```

`buzz agents archived` verifies the snapshot's NIP-11 `self` authorship, event
id, signature, and NIP-70 protection tag; any trust failure is a nonzero exit,
never a false-empty success.

**The agent cannot retire itself out of trouble.** Per `buzz agents archive
--help`: an agent running under `BUZZ_AUTH_TAG` signs as itself, so it can only
satisfy the *self* path (`target == signer`). Archiving a third-party identity
is a human owner/admin action. Rotating a compromised agent key is therefore an
owner task, not something Kara can perform.

Rotation order: mint the new identity and attestation → add the new pubkey to
the channels → archive the old one with `--replaced-by` → remove the old member
rows.

### Revocation — three independent levers

| Lever | Command | Effect |
|-------|---------|--------|
| Channel | `buzz channels remove-member --channel <uuid> --pubkey <hex>` | Drops one channel only |
| Community | `buzz moderation ban --pubkey <hex>` / `timeout` (kinds 9040–9043) | Write-block or full ban across the community |
| Identity | `buzz agents archive <pubkey>` | Retires the identity itself |

`buzz moderation restricted` lists currently-restricted members;
`buzz moderation audit` is the audit trail. Revoking the attestation is the
strongest lever — without it, gate 2 fails and every request 403s — but it is an
owner-side operation.

### Recovery

- **Password-protected key backup (NIP-49).** Desktop can produce an
  `identity.ncryptsec` backup and verify it before you rely on it. Make one at
  provisioning time, not after a loss.
- **Device import** goes over the pairing relay (`wss://pairing.buzz.xyz`) with
  a short-authentication-string confirmation and a NIP-44-encrypted payload —
  the SAS must be compared out of band; that is what stops a relay-side
  man-in-the-middle.
- **A lost private key is unrecoverable.** The attestation, the community
  enrollment, and every channel membership are all bound to that pubkey, so
  losing the key means re-provisioning from step 1 and archiving the old
  identity with `--replaced-by`.

---

## 8. Security checklist

- [ ] Kara has its **own** keypair — not the owner's, not another agent's.
- [ ] The attestation was issued for **this** pubkey (verify: `buzz_identity.py check` passes the identity gate).
- [ ] `BUZZ_PRIVATE_KEY` lives in a mode-`0600` file owned by the service user, outside the repo.
- [ ] Credentials are **not** in argv, prompts, logs, or commit history.
- [ ] `BUZZ_ACP_RESPOND_TO=owner-only` (or `allowlist` with an explicit list).
- [ ] `BUZZ_ACP_ALLOWED_RESPOND_TO` pins the permitted modes so config drift cannot widen access.
- [ ] `buzz channels set-add-policy --policy owner_only` so strangers cannot pull Kara into channels.
- [ ] Channel membership verified against `buzz channels members`, not inferred from a successful read.
- [ ] An `identity.ncryptsec` backup exists and has been verified.
- [ ] Exactly **one** process drives this relay + pubkey pair. Two processes on one identity race each other.
- [ ] The rotation path (`archive --replaced-by`) is understood to require the owner, before it is needed.

---

## Sources

All commands were run against the live relay `wss://ameeradev.communities.buzz.xyz`
on 2026-08-03 with the `Claude` managed identity; no key or signature value was
read or printed.

- `buzz --help`, `buzz agents|channels|users|moderation --help`,
  `buzz agents archive --help`, `buzz agents archived --help` (Buzz CLI, `/home/odai/.local/bin/buzz`)
- Relay NIP-11 document, `https://ameeradev.communities.buzz.xyz/`
- NIP-OA validation rules and relay error codes: strings in the installed `buzz`
  binary (`buzz_sdk::nip_oa::parse_auth_tag`, `validate_conditions`,
  `verify_auth_tag`)
- Managed-agent record layout: `~/.local/share/xyz.block.buzz.app/agents/managed-agents.json`
  (structure only — 14 records, 7 persona + 7 identity)
- `buzz-acp --help` for the inbound access-policy variables
- Repo baseline for the port note: `git grep` over `Kara-Personal-Agent` at `ca0b8b5`
- Prior team research: `RESEARCH/BUZZ_AGENT_CONNECTIVITY_REPORT.md`,
  `GUIDES/HERMES_BUZZ_CREDENTIAL_PASSTHROUGH.md` (Buzz workspace)
