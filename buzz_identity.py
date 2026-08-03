"""Buzz/Nostr identity preflight for Kara (read-only; never reads secret values).

Kara talks to Buzz as its *own* Nostr identity, not as its owner. Four separate
gates must all be satisfied before a single message can be published, and three
of them fail with errors that look almost identical from the outside:

    1. identity            BUZZ_PRIVATE_KEY  -- the keypair every event is signed with
    2. owner attestation   BUZZ_AUTH_TAG     -- NIP-OA tag the owner signed over (1)
    3. relay membership    server-side       -- is that pubkey enrolled in the community
    4. channel membership  server-side       -- is that pubkey a member of the channel

This module runs each gate as its own check so a failure names the gate that
actually broke instead of "403". It is deliberately read-only: it shells out to
the ``buzz`` CLI using an allow-list of query-only subcommands, and it never
reads, stores, or prints ``BUZZ_PRIVATE_KEY`` or an attestation signature. The
private key is a secret; the auth tag is *not* (it is published in the clear in
the tags of every event Kara signs) but it is bound to one pubkey and is useless
without the matching key.

See ``KARA_BUZZ_IDENTITY_PROVISIONING.md`` for the provisioning walkthrough this
tool verifies.

STUDY GUIDE
-----------
* ``parse_auth_tag`` validates the NIP-OA 4-element tag locally, before any
  network call, so a malformed tag is diagnosed offline.
* ``run_checks`` returns a list of ``CheckResult`` records; ``diagnose`` turns
  them into the one remediation that actually matters.
* Every subprocess call goes through ``run_buzz``, which refuses any subcommand
  not in ``READ_ONLY_COMMANDS`` -- the tool cannot mutate relay state by design.
* Key concepts: allow-listing side effects, structured check results, redacting
  secrets at the boundary instead of hoping callers remember to.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

ENV_RELAY_URL = "BUZZ_RELAY_URL"
ENV_PRIVATE_KEY = "BUZZ_PRIVATE_KEY"
ENV_AUTH_TAG = "BUZZ_AUTH_TAG"
ENV_CHANNEL_IDS = "BUZZ_ACP_CHANNELS"

BUZZ_BIN = "buzz"
AUTH_TAG_LABEL = "auth"
PUBKEY_HEX_LENGTH = 64
SIGNATURE_HEX_LENGTH = 128

# Only query subcommands. Anything that writes to the relay -- messages send,
# channels add-member, agents archive, moderation ban -- is intentionally absent
# so a preflight can never change the state it is inspecting.
READ_ONLY_COMMANDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("users", "get"),
        ("channels", "list"),
        ("channels", "members"),
    }
)

Runner = Callable[[Sequence[str]], "CommandOutput"]


class BuzzIdentityError(RuntimeError):
    """Raised when Kara's Buzz identity configuration is missing or malformed."""


@dataclass(frozen=True)
class CommandOutput:
    """One ``buzz`` invocation's result (exit code plus captured streams)."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def error_message(self) -> str:
        """Pull the relay's JSON ``message`` out of stderr, falling back to raw text."""
        raw = (self.stderr or self.stdout).strip()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if isinstance(payload, dict):
            return str(payload.get("message") or raw)
        return raw

    def json(self) -> Any:
        try:
            return json.loads(self.stdout)
        except (ValueError, TypeError) as exc:
            raise BuzzIdentityError(
                f"buzz returned output that is not JSON: {self.stdout[:200]!r}"
            ) from exc


@dataclass(frozen=True)
class AuthTag:
    """A parsed NIP-OA owner attestation.

    ``signature`` is kept so callers can check its shape, never to be printed --
    ``__repr__`` redacts it so an accidental ``print(tag)`` or a traceback cannot
    leak it into a log.
    """

    owner_pubkey: str
    conditions: str
    signature: str = field(repr=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"AuthTag(owner_pubkey={self.owner_pubkey!r}, "
            f"conditions={self.conditions!r}, signature=<redacted>)"
        )


@dataclass(frozen=True)
class CheckResult:
    """One gate's outcome. ``ok=None`` means the check could not run (skipped)."""

    name: str
    ok: bool | None
    detail: str
    remediation: str = ""

    @property
    def status(self) -> str:
        if self.ok is None:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


def is_hex(value: str, length: int) -> bool:
    """True when ``value`` is exactly ``length`` lowercase-or-uppercase hex digits."""
    if len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def redact(name: str) -> str:
    """Describe an environment variable's presence without revealing its value."""
    raw = os.getenv(name, "")
    if not raw.strip():
        return "not set"
    return f"set ({len(raw.strip())} chars)"


def validate_conditions(conditions: str) -> None:
    """Validate the NIP-OA conditions clause (element 2 of the auth tag).

    An empty string means "no conditions". Otherwise it is ``&``-joined clauses
    such as ``created_at<1790000000``; whitespace and empty clauses are rejected
    by the Buzz SDK, so reject them here too rather than discovering it at the
    relay.
    """
    if conditions == "":
        return
    if any(ch.isspace() for ch in conditions):
        raise BuzzIdentityError("auth tag conditions must not contain whitespace")
    if any(clause == "" for clause in conditions.split("&")):
        raise BuzzIdentityError(
            "auth tag conditions have an empty clause (leading, trailing, or double '&')"
        )


def parse_auth_tag(raw: str) -> AuthTag:
    """Parse and structurally validate a ``BUZZ_AUTH_TAG`` JSON value.

    The tag is a 4-element Nostr tag: ``["auth", <owner pubkey>, <conditions>,
    <signature>]``. This checks shape only -- verifying the signature needs
    secp256k1 and the agent pubkey, which the relay and the ``buzz`` CLI already
    do on every request.
    """
    if not raw.strip():
        raise BuzzIdentityError(f"{ENV_AUTH_TAG} is empty")
    try:
        tag = json.loads(raw)
    except ValueError as exc:
        raise BuzzIdentityError(f"{ENV_AUTH_TAG} is not valid JSON: {exc}") from exc
    if not isinstance(tag, list):
        raise BuzzIdentityError("auth tag must be a JSON array")
    if len(tag) != 4:
        raise BuzzIdentityError(f"auth tag must have 4 elements, got {len(tag)}")
    if not all(isinstance(element, str) for element in tag):
        raise BuzzIdentityError("every auth tag element must be a string")

    label, owner_pubkey, conditions, signature = tag
    if label != AUTH_TAG_LABEL:
        raise BuzzIdentityError(f'auth tag label must be "auth" (got "{label}")')
    if not is_hex(owner_pubkey, PUBKEY_HEX_LENGTH):
        raise BuzzIdentityError(
            f"auth tag owner pubkey must be {PUBKEY_HEX_LENGTH}-character hex"
        )
    validate_conditions(conditions)
    if not is_hex(signature, SIGNATURE_HEX_LENGTH):
        raise BuzzIdentityError(
            f"auth tag signature must be {SIGNATURE_HEX_LENGTH}-character hex"
        )
    return AuthTag(owner_pubkey=owner_pubkey, conditions=conditions, signature=signature)


def configured_channel_ids() -> tuple[str, ...]:
    """Channel UUIDs to verify, shared with the running ACP harness."""
    raw = os.getenv(ENV_CHANNEL_IDS, "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def run_buzz(args: Sequence[str], *, timeout: float = 30.0) -> CommandOutput:
    """Run one read-only ``buzz`` subcommand and capture its output.

    Refuses anything outside ``READ_ONLY_COMMANDS`` so this module cannot be
    repurposed -- or accidentally extended -- into something that mutates relay
    state. Credentials are inherited from the environment and never appear in
    argv, which is world-readable through ``/proc`` on Linux.
    """
    args = list(args)
    if len(args) < 2 or (args[0], args[1]) not in READ_ONLY_COMMANDS:
        raise BuzzIdentityError(
            f"refusing to run non-read-only buzz command: {' '.join(args) or '<empty>'}"
        )
    try:
        completed = subprocess.run(
            [BUZZ_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BuzzIdentityError(
            f"'{BUZZ_BIN}' is not on PATH. Install the Buzz CLI before running the preflight."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BuzzIdentityError(
            f"'{BUZZ_BIN} {' '.join(args)}' timed out after {timeout:.0f}s"
        ) from exc
    return CommandOutput(completed.returncode, completed.stdout, completed.stderr)


def check_environment() -> CheckResult:
    """Gate 0: are the three Buzz variables present in this process?"""
    presence = {name: redact(name) for name in (ENV_RELAY_URL, ENV_PRIVATE_KEY, ENV_AUTH_TAG)}
    missing = [name for name, state in presence.items() if state == "not set"]
    detail = ", ".join(f"{name}={state}" for name, state in presence.items())
    if missing:
        return CheckResult(
            "environment",
            False,
            detail,
            f"Set {', '.join(missing)} in the credential file this process reads. "
            "They must be present where Kara's tools actually execute, not only "
            "where the process was launched.",
        )
    return CheckResult("environment", True, detail)


def check_cli() -> CheckResult:
    """Gate 0: is the ``buzz`` CLI installed and on PATH?"""
    path = shutil.which(BUZZ_BIN)
    if path is None:
        return CheckResult(
            "buzz-cli",
            False,
            f"'{BUZZ_BIN}' not found on PATH",
            "Install the Buzz CLI and make sure PATH is exported to the service unit.",
        )
    return CheckResult("buzz-cli", True, f"found at {path}")


def check_auth_tag() -> CheckResult:
    """Gate 2 (offline half): is ``BUZZ_AUTH_TAG`` a structurally valid NIP-OA tag?"""
    raw = os.getenv(ENV_AUTH_TAG, "")
    if not raw.strip():
        return CheckResult(
            "auth-tag-structure",
            False,
            f"{ENV_AUTH_TAG} is not set",
            "This relay enforces owner attestation; without it every request "
            "fails with 403 relay_membership_required.",
        )
    try:
        tag = parse_auth_tag(raw)
    except BuzzIdentityError as exc:
        return CheckResult(
            "auth-tag-structure",
            False,
            str(exc),
            "Re-issue the attestation from the owner's Buzz Desktop; do not "
            "hand-edit the tag.",
        )
    conditions = tag.conditions or "(none)"
    return CheckResult(
        "auth-tag-structure",
        True,
        f"owner={tag.owner_pubkey} conditions={conditions} signature=<redacted, 128 hex>",
    )


def check_identity(runner: Runner) -> tuple[CheckResult, str]:
    """Gate 1: does the relay resolve ``BUZZ_PRIVATE_KEY`` to a usable identity?

    Returns the check result and the agent's own pubkey (empty when unresolved),
    which the channel-membership gate needs.
    """
    result = runner(["users", "get"])
    if not result.ok:
        return (
            CheckResult(
                "identity",
                False,
                result.error_message(),
                "The key is missing or the attestation does not match it. An auth tag "
                "is signed over one agent pubkey and cannot be moved to another key.",
            ),
            "",
        )
    try:
        profiles = result.json()
    except BuzzIdentityError as exc:
        return (
            CheckResult("identity", False, str(exc), "Unexpected CLI output; check the CLI version."),
            "",
        )
    if not isinstance(profiles, list) or not profiles:
        return (
            CheckResult(
                "identity",
                False,
                "relay returned no profile for this key",
                "Publish a profile with `buzz users set-profile` after enrolling the identity.",
            ),
            "",
        )
    profile = profiles[0]
    pubkey = str(profile.get("pubkey", ""))
    name = str(profile.get("display_name") or "(no display name)")
    return CheckResult("identity", True, f"{name} <{pubkey}>"), pubkey


def check_relay_membership(runner: Runner) -> CheckResult:
    """Gate 3: is this pubkey enrolled in the relay's community?"""
    result = runner(["channels", "list"])
    if not result.ok:
        message = result.error_message()
        if "relay_membership_required" in message:
            return CheckResult(
                "relay-membership",
                False,
                message,
                "The identity is not enrolled in this community, or the request "
                "carried no valid attestation. Enrollment is an owner/admin "
                "action in Buzz Desktop.",
            )
        return CheckResult("relay-membership", False, message, "See the relay error above.")
    try:
        channels = result.json()
    except BuzzIdentityError as exc:
        return CheckResult("relay-membership", False, str(exc), "Unexpected CLI output.")
    return CheckResult("relay-membership", True, f"{len(channels)} channel(s) visible")


def check_channel_membership(
    runner: Runner, channel_ids: Sequence[str], self_pubkey: str
) -> list[CheckResult]:
    """Gate 4: is this pubkey an actual member row in each configured channel?

    Worth doing explicitly: reading a channel Kara is *not* a member of returns
    an empty array and exit code 0, which is indistinguishable from an empty
    channel. Membership has to be asserted against the member list.
    """
    if not channel_ids:
        return [
            CheckResult(
                "channel-membership",
                None,
                f"{ENV_CHANNEL_IDS} is not set",
                f"Set {ENV_CHANNEL_IDS} to the channel UUIDs Kara should work in "
                "so this preflight can verify membership.",
            )
        ]
    if not self_pubkey:
        return [
            CheckResult(
                "channel-membership",
                None,
                "own pubkey unknown (identity check did not pass)",
                "Fix the identity gate first.",
            )
        ]

    results: list[CheckResult] = []
    for channel_id in channel_ids:
        result = runner(["channels", "members", "--channel", channel_id])
        name = f"channel-membership[{channel_id}]"
        if not result.ok:
            results.append(
                CheckResult(name, False, result.error_message(), "Check the channel UUID.")
            )
            continue
        try:
            members = result.json()
        except BuzzIdentityError as exc:
            results.append(CheckResult(name, False, str(exc), "Unexpected CLI output."))
            continue
        entry = next(
            (m for m in members if isinstance(m, dict) and m.get("pubkey") == self_pubkey),
            None,
        )
        if entry is None:
            results.append(
                CheckResult(
                    name,
                    False,
                    f"not a member ({len(members)} other member(s))",
                    "An owner or admin must run: buzz channels add-member "
                    f"--channel {channel_id} --pubkey <agent pubkey> --role bot",
                )
            )
            continue
        results.append(CheckResult(name, True, f"member with role '{entry.get('role', 'member')}'"))
    return results


def run_checks(runner: Runner | None = None) -> list[CheckResult]:
    """Run every gate in dependency order and return the collected results."""
    active_runner: Runner = runner if runner is not None else (lambda args: run_buzz(args))

    results = [check_environment(), check_cli(), check_auth_tag()]
    if not results[1].ok:
        # Without the CLI there is nothing to ask the relay with.
        results.append(
            CheckResult("identity", None, "skipped: buzz CLI unavailable", "Install the Buzz CLI.")
        )
        results.append(
            CheckResult("relay-membership", None, "skipped: buzz CLI unavailable", "Install the Buzz CLI.")
        )
        results.extend(check_channel_membership(active_runner, (), ""))
        return results

    identity, self_pubkey = check_identity(active_runner)
    results.append(identity)
    results.append(check_relay_membership(active_runner))
    results.extend(check_channel_membership(active_runner, configured_channel_ids(), self_pubkey))
    return results


def diagnose(results: Sequence[CheckResult]) -> str:
    """Return the single most useful next action, or a success line."""
    for result in results:
        if result.ok is False:
            return f"{result.name}: {result.remediation or result.detail}"
    if any(result.ok is None for result in results):
        return "Core gates pass; some checks were skipped (see SKIP rows)."
    return "All gates pass. Kara's Buzz identity is fully provisioned."


def format_report(results: Sequence[CheckResult]) -> str:
    """Render the human-readable preflight report."""
    width = max(len(result.name) for result in results)
    lines = [f"{result.status:<4}  {result.name:<{width}}  {result.detail}" for result in results]
    lines.append("")
    lines.append(diagnose(results))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Kara's Buzz identity, attestation, and membership (read-only)"
    )
    parser.add_argument("command", choices=["check"], nargs="?", default="check")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    try:
        results = run_checks()
    except BuzzIdentityError as exc:
        print(str(exc))
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "checks": [
                        {
                            "name": r.name,
                            "status": r.status,
                            "detail": r.detail,
                            "remediation": r.remediation,
                        }
                        for r in results
                    ],
                    "diagnosis": diagnose(results),
                },
                indent=2,
            )
        )
    else:
        print(format_report(results))

    return 0 if all(result.ok is not False for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
