"""Email tools via the Himalaya CLI (IMAP/SMTP).

Kara does not implement email protocols herself — she shells out to
``himalaya``, a trusted Rust CLI (https://github.com/pimalaya/himalaya).
Configure accounts with ``himalaya account configure``; credentials live in
Himalaya's config file, not in Kara's ``.env``.

Targets Himalaya v1.x, whose CLI shape is:
  - JSON output:      ``-o json`` (NOT ``--json``)
  - list folders:     ``folder list``
  - list envelopes:   ``envelope list -f <folder> -p <page>``
  - search:           ``envelope list -f <folder> <query...>`` (query is positional)
  - read a message:   ``message read -f <folder> <id>``
  - add a flag:       ``flag add -f <folder> <id> <flag>`` (id + flag are positional)
  - send raw message: ``message send`` (raw RFC 5322 piped via stdin)

STUDY GUIDE
-----------
* Wraps an external CLI with ``subprocess.run`` and parses JSON output.
* Returns setup instructions when Himalaya is missing or not configured.
* Gates outbound mail behind ``EMAIL_SEND_ENABLED`` for safety on Telegram.
* Key concepts: subprocess, JSON parsing, optional tools, environment flags.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import config  # loads personal_agent/.env (HIMALAYA_BIN, etc.)

# LEARN: Default timeout for IMAP/SMTP network calls — avoids hanging forever.
DEFAULT_TIMEOUT = float(os.getenv("HIMALAYA_TIMEOUT", "60"))
MAX_BODY_CHARS = int(os.getenv("EMAIL_READ_MAX_CHARS", "12000"))


def _himalaya_bin() -> str:
    return os.getenv("HIMALAYA_BIN", "himalaya").strip() or "himalaya"


def _himalaya_config_path() -> Path | None:
    configured = os.getenv("HIMALAYA_CONFIG", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    # LEARN: Himalaya's default config lives in different spots per-OS.
    for candidate in (
        Path.home() / ".config" / "himalaya" / "config.toml",
        Path(os.environ.get("APPDATA", "")) / "himalaya" / "config.toml",
        Path.home() / "AppData" / "Roaming" / "himalaya" / "config.toml",
    ):
        if candidate.exists():
            return candidate
    return None


def _resolve_himalaya() -> str | None:
    configured = _himalaya_bin()
    if Path(configured).exists():
        return configured
    return shutil.which(configured)


def _not_ready_message() -> str:
    if _resolve_himalaya() is None:
        return (
            "Himalaya CLI is not installed. Install from "
            "https://github.com/pimalaya/himalaya/releases "
            "(Windows: download himalaya.x86_64-windows.zip). "
            "Then run: himalaya account configure"
        )
    if _himalaya_config_path() is None:
        return (
            "Himalaya is installed but not configured. Run: himalaya account configure "
            "(config is usually ~/.config/himalaya/config.toml). "
            "Optional: set HIMALAYA_CONFIG or HIMALAYA_ACCOUNT in personal_agent/.env"
        )
    return ""


def _run_himalaya(
    *args: str, input_text: str | None = None, json_output: bool = False
) -> tuple[int, str, str]:
    # LEARN: Build argv as a list — safer than a shell string (no injection via subject lines).
    bin_path = _resolve_himalaya()
    if not bin_path:
        raise RuntimeError(_not_ready_message())

    cmd: list[str] = [bin_path, *args]
    # LEARN: config/account/output are appended as trailing options (valid subcommand flags).
    config_path = os.getenv("HIMALAYA_CONFIG", "").strip()
    if config_path:
        cmd.extend(["-c", str(Path(config_path).expanduser())])
    account = os.getenv("HIMALAYA_ACCOUNT", "").strip()
    if account:
        cmd.extend(["-a", account])
    if json_output:
        cmd.extend(["-o", "json"])

    result = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _require_ready() -> str | None:
    msg = _not_ready_message()
    return msg or None


def _format_address(addr: Any) -> str:
    # LEARN: Himalaya v1 gives a single object {"name": ..., "addr": ...} for from/to.
    if isinstance(addr, dict):
        name = (addr.get("name") or "").strip()
        email = (addr.get("addr") or addr.get("email") or "").strip()
        return f"{name} <{email}>" if name else (email or "unknown")
    if isinstance(addr, list):
        return ", ".join(_format_address(a) for a in addr) or "unknown"
    return str(addr) if addr else "unknown"


def _format_envelopes(envelopes: list[dict[str, Any]]) -> str:
    if not envelopes:
        return "No messages found."
    lines = [f"Found {len(envelopes)} message(s):"]
    for env in envelopes:
        msg_id = env.get("id", "?")
        subject = (env.get("subject") or "(no subject)").strip()
        sender = _format_address(env.get("from"))
        date = env.get("date") or ""
        flags = env.get("flags") or []
        flag_txt = f" [{', '.join(str(f) for f in flags)}]" if flags else " [unread]"
        attach = " [attachment]" if env.get("has_attachment") or env.get("has-attachment") else ""
        lines.append(f"- id={msg_id} | {date} | from {sender} | {subject}{flag_txt}{attach}")
    return "\n".join(lines)


def _parse_json(out: str) -> Any:
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _truncate(text: str, limit: int = MAX_BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... (truncated, {len(text) - limit} more chars)"


def _send_enabled() -> bool:
    return os.getenv("EMAIL_SEND_ENABLED", "").strip().lower() in ("1", "true", "yes")


def email_status() -> str:
    """
    Check whether email (Himalaya CLI) is installed and configured for Kara.

    Returns:
        Ready status or setup instructions.
    """
    err = _require_ready()
    if err:
        return err
    code, out, err_out = _run_himalaya("--version")
    version = out or err_out or "unknown version"
    if code != 0:
        return f"Himalaya failed: {err_out or out or 'unknown error'}"
    config_path = _himalaya_config_path()
    account = os.getenv("HIMALAYA_ACCOUNT", "").strip() or "(default account)"
    send = "enabled" if _send_enabled() else "disabled (set EMAIL_SEND_ENABLED=true to allow sending)"
    return (
        f"Email ready via {version}. "
        f"Config: {config_path}. Account: {account}. Sending: {send}."
    )


def email_list_mailboxes() -> str:
    """
    List email folders/mailboxes (INBOX, Sent, etc.) for the configured account.

    Returns:
        Folder names, or an error/setup message.
    """
    err = _require_ready()
    if err:
        return err
    code, out, err_out = _run_himalaya("folder", "list", json_output=True)
    if code != 0:
        return f"Error listing folders: {err_out or out or 'unknown error'}"
    data = _parse_json(out)
    if not isinstance(data, list):
        return out or "No folder data returned."
    names = [str(item.get("name")) for item in data if isinstance(item, dict) and item.get("name")]
    if not names:
        return "No folders found."
    return "Folders:\n" + "\n".join(f"- {name}" for name in names)


def email_list_envelopes(mailbox: str = "INBOX", page: int = 1) -> str:
    """
    List recent emails in a folder, newest first.

    Args:
        mailbox: Folder name (default INBOX).
        page: Page number for pagination (default 1).

    Returns:
        A summary list with message ids, dates, senders, and subjects.
    """
    err = _require_ready()
    if err:
        return err
    try:
        page_num = max(1, int(page))
    except (TypeError, ValueError):
        page_num = 1
    code, out, err_out = _run_himalaya(
        "envelope",
        "list",
        "-f",
        mailbox.strip() or "INBOX",
        "-p",
        str(page_num),
        json_output=True,
    )
    if code != 0:
        return f"Error listing mail in {mailbox}: {err_out or out or 'unknown error'}"
    data = _parse_json(out)
    if not isinstance(data, list):
        return out or "No messages returned."
    return _format_envelopes(data)


def email_search(query: str, mailbox: str = "INBOX") -> str:
    """
    Search emails by sender, subject, date, body text, flags, etc.

    Args:
        query: Search query, e.g. 'from alice and subject invoice', 'after 2026-01-01',
            'subject report order by date desc'.
        mailbox: Folder to search (default INBOX).

    Returns:
        Matching message summaries with ids for follow-up reads.
    """
    err = _require_ready()
    if err:
        return err
    query = query.strip()
    if not query:
        return "Error: search query is required."
    # LEARN: Himalaya v1 has no separate 'search' — the query is a positional
    # argument to 'envelope list'. Split into tokens like a shell would.
    code, out, err_out = _run_himalaya(
        "envelope",
        "list",
        "-f",
        mailbox.strip() or "INBOX",
        *query.split(),
        json_output=True,
    )
    if code != 0:
        hint = ""
        if "sort" in (err_out or out or "").lower():
            hint = " (This IMAP server may not support server-side search/sort; try email_list_envelopes.)"
        return f"Error searching mail: {err_out or out or 'unknown error'}{hint}"
    data = _parse_json(out)
    if not isinstance(data, list):
        return out or "No search results."
    return _format_envelopes(data)


def email_read(message_id: int, mailbox: str = "INBOX") -> str:
    """
    Read the full text of an email by its folder-local id (from list/search results).
    Note: reading marks the message as seen.

    Args:
        message_id: Numeric message id from email_list_envelopes or email_search.
        mailbox: Folder the message lives in (default INBOX).

    Returns:
        Headers and body text of the message.
    """
    err = _require_ready()
    if err:
        return err
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return f"Error: invalid message_id '{message_id}'."
    code, out, err_out = _run_himalaya(
        "message",
        "read",
        "-f",
        mailbox.strip() or "INBOX",
        str(mid),
    )
    if code != 0:
        return f"Error reading message {mid}: {err_out or out or 'unknown error'}"
    return _truncate(out or "(empty message)")


def email_mark_seen(message_id: int, mailbox: str = "INBOX") -> str:
    """
    Mark an email as read (adds the seen flag).

    Args:
        message_id: Numeric message id.
        mailbox: Folder containing the message (default INBOX).

    Returns:
        Confirmation or error message.
    """
    err = _require_ready()
    if err:
        return err
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return f"Error: invalid message_id '{message_id}'."
    # LEARN: In Himalaya v1, id and flag are both positional args to 'flag add'.
    code, out, err_out = _run_himalaya(
        "flag",
        "add",
        "-f",
        mailbox.strip() or "INBOX",
        str(mid),
        "seen",
    )
    if code != 0:
        return f"Error marking message {mid} seen: {err_out or out or 'unknown error'}"
    return out or f"Marked message {mid} in {mailbox} as seen."


def _build_raw_message(to: str, subject: str, body: str, cc: str, bcc: str) -> str:
    # LEARN: EmailMessage builds a valid RFC 5322 message we can pipe to 'message send'.
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    from_addr = os.getenv("HIMALAYA_FROM", "").strip()
    if from_addr:
        msg["From"] = from_addr
    msg.set_content(body)
    return msg.as_string()


def email_send(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> str:
    """
    Send a plain-text email. Requires EMAIL_SEND_ENABLED=true in .env (safety gate for Telegram).

    Args:
        to: Recipient email address(es), comma-separated.
        subject: Email subject line.
        body: Plain-text message body.
        cc: Optional CC recipients, comma-separated.
        bcc: Optional BCC recipients, comma-separated.

    Returns:
        Send confirmation, a preview if sending is disabled, or an error message.
    """
    err = _require_ready()
    if err:
        return err
    to = to.strip()
    subject = subject.strip()
    body = body.strip()
    if not to or not subject or not body:
        return "Error: to, subject, and body are all required."

    raw = _build_raw_message(to, subject, body, cc.strip(), bcc.strip())

    if not _send_enabled():
        preview = f"To: {to}\nSubject: {subject}\n"
        if cc.strip():
            preview += f"Cc: {cc.strip()}\n"
        preview += f"\n{body}"
        return (
            "Sending is DISABLED. Draft preview:\n\n"
            f"{preview}\n\n"
            "Set EMAIL_SEND_ENABLED=true in personal_agent/.env to allow Kara to send mail."
        )

    # LEARN: Pipe the raw RFC 5322 message to 'message send' via stdin.
    code, out, err_out = _run_himalaya("message", "send", input_text=raw)
    if code != 0:
        return f"Error sending email: {err_out or out or 'unknown error'}"
    return out or f"Email sent to {to} with subject '{subject}'."
