"""Small, safety-gated adapter for the already-installed cua-driver.

Unlike Hermes' full backend this uses cua-driver's stable one-shot CLI surface.
Read-only discovery/capture calls run immediately. Input actions use a two-turn
approval token, preventing the model from approving an action in the same turn
that requested it.
"""
from __future__ import annotations

import contextvars
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any

_SESSION_ID = f"kara-{os.getpid()}"
_READ_ONLY_ACTIONS = {"list_apps", "list_windows", "capture"}
_INPUT_ACTIONS = {
    "click",
    "double_click",
    "right_click",
    "type",
    "key",
    "scroll",
    "focus",
    "bring_to_front",
}
_FOREGROUND_SETTLE_SECONDS = float(os.getenv("KARA_CUA_FOCUS_SETTLE_SECONDS", "0.2"))
_request_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "kara_computer_request", default={"session_key": "unknown", "user_input": ""}
)
_pending_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}
_last_targets: dict[str, dict[str, Any]] = {}


class DriverCallError(RuntimeError):
    """A cua-driver failure that preserves structured output when available."""

    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


def set_computer_request_context(session_key: str, user_input: str) -> None:
    """Bind CUA approval checks to the current user-authored turn."""
    _request_context.set({"session_key": session_key, "user_input": user_input})


def _driver_command() -> str | None:
    configured = (
        os.getenv("KARA_CUA_DRIVER_CMD", "").strip()
        or os.getenv("HERMES_CUA_DRIVER_CMD", "").strip()
        or "cua-driver"
    )
    return shutil.which(configured)


def _child_env() -> dict[str, str]:
    """Do not pass provider/bot credentials to the third-party driver process."""
    blocked_fragments = (
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "client_secret",
        "telegram_bot_token",
        "password",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(fragment in key.casefold() for fragment in blocked_fragments)
    }
    if os.getenv("KARA_CUA_TELEMETRY", "").strip().lower() not in {"1", "true", "yes"}:
        env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
    return env


def _call_driver(tool: str, args: dict[str, Any], timeout: float = 35.0) -> dict[str, Any]:
    driver = _driver_command()
    if not driver:
        raise RuntimeError(
            "cua-driver is not on PATH. Set KARA_CUA_DRIVER_CMD to its full executable path."
        )
    command = [driver, "call", tool, json.dumps(args, ensure_ascii=False)]
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=_child_env(),
        **kwargs,
    )
    output = (completed.stdout or "").strip()
    parsed: Any = None
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        pass
    if completed.returncode != 0:
        detail = (completed.stderr or output or "unknown driver error").strip()
        raise DriverCallError(
            f"cua-driver {tool} failed: {detail[:1000]}",
            parsed if isinstance(parsed, dict) else {"stderr": detail[:1000]},
        )
    if not isinstance(parsed, dict):
        raise DriverCallError(
            f"cua-driver {tool} returned invalid JSON: {output[:500]}"
        )
    if parsed.get("isError") is True:
        raise DriverCallError(
            str(parsed.get("error") or parsed.get("message") or parsed)[:1000], parsed
        )
    return parsed


def _apps() -> list[dict[str, Any]]:
    data = _call_driver("list_apps", {})
    apps = data.get("apps") or (data.get("structuredContent") or {}).get("apps") or []
    return [item for item in apps if isinstance(item, dict)]


def _windows() -> list[dict[str, Any]]:
    data = _call_driver("list_windows", {})
    windows = data.get("windows") or (data.get("structuredContent") or {}).get("windows") or []
    return [item for item in windows if isinstance(item, dict)]


def _current_session_key() -> str:
    return _request_context.get().get("session_key") or "unknown"


def _window_target(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "pid": int(window.get("pid") or 0),
        "window_id": int(window.get("window_id") or 0),
        "title": str(window.get("title") or ""),
        "app_name": str(window.get("app_name") or ""),
    }


def _target_identity(target: dict[str, Any]) -> dict[str, Any]:
    """Fields that make an approval specific to one concrete top-level window."""
    return {
        "pid": int(target.get("pid") or 0),
        "window_id": int(target.get("window_id") or 0),
        "title": str(target.get("title") or ""),
        "app_name": str(target.get("app_name") or ""),
    }


def _app_stem(value: Any) -> str:
    name = os.path.basename(str(value or "")).casefold()
    for suffix in (".exe", ".app"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _window_area(window: dict[str, Any]) -> int:
    bounds = window.get("bounds") or {}
    if isinstance(bounds, dict):
        width = bounds.get("width") or bounds.get("w") or 0
        height = bounds.get("height") or bounds.get("h") or 0
    elif isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
        width, height = bounds[2], bounds[3]
    else:
        return 0
    try:
        return max(0, int(width)) * max(0, int(height))
    except (TypeError, ValueError):
        return 0


def _window_match_score(
    window: dict[str, Any],
    needle: str,
    active_pids: set[int],
    foreground: dict[str, int] | None,
) -> tuple[int, int, int, int, int, int, int]:
    title = str(window.get("title") or "").strip().casefold()
    app_name = _app_stem(window.get("app_name"))
    score = 0
    if app_name == needle:
        score = 700
    elif title == needle:
        score = 600
    elif title.startswith(needle):
        score = 450
    elif app_name.startswith(needle):
        score = 400
    elif needle in app_name:
        score = 300
    elif needle in title:
        score = 250
    owner_pid = int(window.get("pid") or 0)
    hwnd = int(window.get("window_id") or 0)
    exact_foreground = int(
        bool(
            foreground
            and owner_pid == int(foreground.get("pid") or 0)
            and hwnd == int(foreground.get("window_id") or 0)
        )
    )
    return (
        score,
        exact_foreground,
        int(owner_pid in active_pids),
        int(window.get("is_on_screen") is not False),
        _window_area(window),
        -int(window.get("z_index") or 0),
        hwnd,
    )


def _resolve_target(app: str, pid: int, window_id: int) -> dict[str, Any]:
    session_key = _current_session_key()
    requested_pid = int(pid or 0)
    requested_window = int(window_id or 0)
    windows = _windows()

    if requested_window:
        for window in windows:
            if int(window.get("window_id") or 0) == requested_window:
                owner = int(window.get("pid") or requested_pid or 0)
                if requested_pid and owner != requested_pid:
                    raise ValueError("window_id does not belong to the requested pid.")
                target = _window_target(window)
                _last_targets[session_key] = target
                return target
        raise ValueError(f"Window {requested_window} was not found.")

    if app.strip():
        needle = app.strip().casefold()
        apps = _apps()
        foreground = _foreground_window()
        active_pids = {
            int(item.get("pid") or 0) for item in apps if item.get("running") and item.get("active")
        }
        matched_windows = [
            window
            for window in windows
            if _window_match_score(window, needle, active_pids, foreground)[0]
        ]
        if matched_windows:
            matched_windows.sort(
                key=lambda item: _window_match_score(
                    item, needle, active_pids, foreground
                ),
                reverse=True,
            )
            chosen = matched_windows[0]
            target = _window_target(chosen)
            _last_targets[session_key] = target
            return target
        matched_apps = [
            item
            for item in apps
            if item.get("running") and needle in _app_stem(item.get("name"))
        ]
        if matched_apps:
            requested_pid = int(matched_apps[0].get("pid") or 0)

    if not app.strip() and not requested_pid:
        previous = _last_targets.get(session_key)
        if previous:
            for window in windows:
                if int(window.get("window_id") or 0) == int(previous.get("window_id") or 0):
                    current = _window_target(window)
                    if int(current["pid"]) != int(previous.get("pid") or 0):
                        raise ValueError("The previously selected window now belongs to another process.")
                    _last_targets[session_key] = current
                    return current
            raise ValueError("The previously selected window no longer exists.")

    if not requested_pid and not app.strip():
        active = next(
            (item for item in _apps() if item.get("running") and item.get("active")), None
        )
        if active:
            requested_pid = int(active.get("pid") or 0)

    if requested_pid:
        candidates = [window for window in windows if int(window.get("pid") or 0) == requested_pid]
        if candidates:
            foreground = _foreground_window()
            candidates.sort(
                key=lambda item: (
                    int(
                        bool(
                            foreground
                            and int(item.get("pid") or 0)
                            == int(foreground.get("pid") or 0)
                            and int(item.get("window_id") or 0)
                            == int(foreground.get("window_id") or 0)
                        )
                    ),
                    int(item.get("is_on_screen") is not False),
                    _window_area(item),
                    -int(item.get("z_index") or 0),
                    int(item.get("window_id") or 0),
                ),
                reverse=True,
            )
            chosen = candidates[0]
            target = _window_target(chosen)
            _last_targets[session_key] = target
            return target
        raise ValueError(f"No visible window was found for pid {requested_pid}.")
    raise ValueError("Specify an app, pid, or window_id (or call capture first).")


def _trimmed(value: Any, *, max_chars: int = 14000) -> Any:
    """Remove image payloads and cap text before returning driver data to the model."""
    if isinstance(value, dict):
        return {
            key: _trimmed(item, max_chars=max_chars)
            for key, item in value.items()
            if key.casefold()
            not in {"screenshot", "image", "image_base64", "screenshot_base64", "data_base64"}
        }
    if isinstance(value, list):
        return [_trimmed(item, max_chars=max_chars) for item in value[:250]]
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"\n... ({len(value) - max_chars} chars truncated)"
    return value


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _failure(
    code: str,
    message: str,
    *,
    target: dict[str, Any] | None = None,
    details: Any = None,
    status: str = "failed",
    action_sent: bool | None = False,
) -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "status": status,
        "action_sent": action_sent,
        "error": {"code": code, "message": message},
    }
    if target:
        payload["target"] = _target_identity(target)
    if details is not None:
        payload["details"] = _trimmed(details)
    return _json_result(payload)


def _success(
    action: str,
    target: dict[str, Any],
    driver_result: dict[str, Any] | None = None,
    foreground: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "ok": True,
        "status": "completed",
        "action": action,
        "action_sent": True,
        "target": _target_identity(target),
    }
    if foreground is not None:
        payload["foreground"] = _trimmed(foreground)
    if driver_result is not None:
        payload["driver"] = _trimmed(driver_result)
    return _json_result(payload)


def _driver_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [result]
    for key in ("structuredContent", "data", "result"):
        value = result.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def _driver_field(result: dict[str, Any], name: str) -> Any:
    for candidate in _driver_dicts(result):
        if name in candidate:
            return candidate[name]
    return None


def _parse_window_id(value: Any) -> int:
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _foreground_window() -> dict[str, int] | None:
    """Return the exact Windows foreground HWND/PID without changing focus."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner_pid))
        return {"pid": int(owner_pid.value), "window_id": hwnd}
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _is_exact_foreground(target: dict[str, Any], current: dict[str, int] | None) -> bool:
    return bool(
        current
        and int(current.get("pid") or 0) == int(target.get("pid") or 0)
        and int(current.get("window_id") or 0) == int(target.get("window_id") or 0)
    )


def _current_window_identity(target: dict[str, Any]) -> dict[str, Any] | None:
    for window in _windows():
        if int(window.get("window_id") or 0) == int(target.get("window_id") or 0):
            return _window_target(window)
    return None


def _ensure_foreground(target: dict[str, Any]) -> dict[str, Any]:
    """Persistently foreground one exact window and fail closed if verification fails."""
    initial = _foreground_window()
    if _is_exact_foreground(target, initial):
        return {
            "ok": True,
            "verified": True,
            "activated": False,
            "initial": initial,
            "final": initial,
        }

    activation = _call_driver(
        "bring_to_front",
        {"pid": int(target["pid"]), "window_id": int(target["window_id"])},
    )
    if _FOREGROUND_SETTLE_SECONDS > 0:
        time.sleep(_FOREGROUND_SETTLE_SECONDS)

    current_identity = _current_window_identity(target)
    if current_identity != _target_identity(target):
        return {
            "ok": False,
            "code": "target_changed_after_approval",
            "message": "The approved window identity changed before input could be delivered.",
            "initial": initial,
            "current_target": current_identity,
            "activation": _trimmed(activation),
        }

    final = _foreground_window()
    landed = _driver_field(activation, "landed_on_target")
    now_hwnd = _driver_field(activation, "now_fg_hwnd")
    if final is not None:
        verified = _is_exact_foreground(target, final)
    else:
        verified = landed is True and (
            _parse_window_id(now_hwnd) == int(target.get("window_id") or 0)
        )

    if not verified:
        return {
            "ok": False,
            "code": "foreground_activation_failed",
            "message": (
                "The operating system did not foreground the approved target window. No input was sent. "
                "Close any modal/secure-desktop prompt or click the target window manually, "
                "then request the action again for a fresh approval."
            ),
            "initial": initial,
            "final": final,
            "activation": _trimmed(activation),
        }
    return {
        "ok": True,
        "verified": True,
        "activated": True,
        "initial": initial,
        "final": final,
        "activation": _trimmed(activation),
    }


def _is_background_unavailable(exc: DriverCallError) -> bool:
    text = f"{exc} {json.dumps(exc.payload, ensure_ascii=False)}".casefold()
    return "background_unavailable" in text or "background delivery is not available" in text


def _approval_signature(action: str, intent: dict[str, Any]) -> str:
    return json.dumps({"action": action, **intent}, sort_keys=True, ensure_ascii=False)


def _approval_gate(
    action: str,
    intent: dict[str, Any],
    target: dict[str, Any],
    approval_token: str,
    summary: str,
) -> str | None:
    context = _request_context.get()
    session_key = context.get("session_key") or "unknown"
    user_input = context.get("user_input") or ""
    signature = _approval_signature(action, intent)
    identity = _target_identity(target)
    now = time.time()
    supplied = approval_token.strip().lower()

    with _pending_lock:
        expired = [token for token, item in _pending.items() if now - item["created"] > 600]
        for token in expired:
            _pending.pop(token, None)
        if supplied:
            pending = _pending.get(supplied)
            if not pending or pending.get("session_key") != session_key:
                return _failure(
                    "approval_invalid",
                    "The approval token is unknown, expired, or belongs to another session.",
                    target=target,
                )
            if pending.get("signature") != signature:
                _pending.pop(supplied, None)
                return _failure(
                    "approval_intent_changed",
                    "The requested action or arguments changed after approval was requested.",
                    target=target,
                )
            if pending.get("target") != identity:
                approved_target = pending.get("target")
                _pending.pop(supplied, None)
                return _failure(
                    "approval_target_changed",
                    "The resolved target window changed after approval was requested. No action ran.",
                    target=target,
                    details={"approved_target": approved_target, "current_target": identity},
                )
            phrase = f"approve {supplied}"
            if user_input.strip().casefold() != phrase:
                return _failure(
                    "approval_not_confirmed",
                    f"Approval required. The user must reply exactly: {phrase}",
                    target=target,
                    status="approval_required",
                )
            _pending.pop(supplied, None)
            return None

        token = secrets.token_hex(3)
        phrase = f"approve {token}"
        _pending[token] = {
            "created": now,
            "session_key": session_key,
            "signature": signature,
            "target": identity,
        }
    return _json_result(
        {
            "ok": False,
            "status": "approval_required",
            "action_sent": False,
            "message": f"Approval required before I {summary}.",
            "target": identity,
            "approval": {
                "token": token,
                "phrase": phrase,
                "expires_in_seconds": 600,
                "instructions": f"Ask the user to reply exactly: {phrase}",
            },
        }
    )


def _pending_target_snapshot(approval_token: str) -> dict[str, Any] | None:
    """Return the exact target captured when this session's token was issued."""
    supplied = approval_token.strip().lower()
    if not supplied:
        return None
    session_key = _current_session_key()
    now = time.time()
    with _pending_lock:
        pending = _pending.get(supplied)
        if (
            not pending
            or pending.get("session_key") != session_key
            or now - pending["created"] > 600
        ):
            if pending and now - pending["created"] > 600:
                _pending.pop(supplied, None)
            return None
        target = pending.get("target")
        return dict(target) if isinstance(target, dict) else None


def _compact_apps() -> str:
    running = [item for item in _apps() if item.get("running")]
    rows = [
        {
            "name": item.get("name"),
            "pid": item.get("pid"),
            "active": bool(item.get("active")),
        }
        for item in running
    ]
    return json.dumps({"running_apps": rows}, indent=2, ensure_ascii=False)


def _compact_windows() -> str:
    rows = [
        {
            "app": item.get("app_name"),
            "title": item.get("title"),
            "pid": item.get("pid"),
            "window_id": item.get("window_id"),
            "on_screen": item.get("is_on_screen"),
        }
        for item in _windows()
    ]
    return json.dumps({"windows": rows}, indent=2, ensure_ascii=False)


def _verify_bound_target(target: dict[str, Any]) -> dict[str, Any]:
    current = _current_window_identity(target)
    if current != _target_identity(target):
        return {
            "ok": False,
            "code": "target_changed_after_approval",
            "message": "The approved target window changed before input delivery. No input was sent.",
            "current_target": current,
        }
    return {"ok": True}


def _driver_reported_failure(result: dict[str, Any]) -> str | None:
    for candidate in _driver_dicts(result):
        if candidate.get("isError") is True or candidate.get("ok") is False or candidate.get("success") is False:
            return str(candidate.get("error") or candidate.get("message") or candidate)
    return None


def _delivery_effect(result: dict[str, Any]) -> str:
    return str(_driver_field(result, "effect") or "").strip().casefold()


def _recommended_escalation(result: dict[str, Any]) -> str:
    for candidate in _driver_dicts(result):
        escalation = candidate.get("escalation")
        if isinstance(escalation, dict):
            return str(escalation.get("recommended") or "").strip().casefold()
    return ""


def _execute_input_action(
    driver_tool: str,
    args: dict[str, Any],
    target: dict[str, Any],
    *,
    keyboard: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run input background-first and follow only the driver's verified escalation rung.

    Action-scoped foreground delivery activates, acts, and restores. It must not
    be replaced by persistent ``bring_to_front``. An unverifiable result is not
    retried because duplicate typing/clicking can be destructive.
    """
    del keyboard  # Kept in the private signature for compatibility with callers/tests.
    del target
    foreground: dict[str, Any] | None = None
    try:
        result = _call_driver(driver_tool, {**args, "delivery_mode": "background"})
    except DriverCallError as exc:
        if not _is_background_unavailable(exc):
            raise
        result = _call_driver(driver_tool, {**args, "delivery_mode": "foreground"})
        foreground = {"mode": "action_scoped", "reason": "background_unavailable"}

    if (
        _delivery_effect(result) == "suspected_noop"
        and _recommended_escalation(result) == "foreground"
    ):
        result = _call_driver(driver_tool, {**args, "delivery_mode": "foreground"})
        foreground = {"mode": "action_scoped", "reason": "suspected_noop"}

    failure = _driver_reported_failure(result)
    if failure:
        raise DriverCallError(failure, result)
    return result, foreground


def _normalized_modifiers(modifiers: list[str] | str | None) -> list[str]:
    if isinstance(modifiers, str):
        values = modifiers.split(",")
    else:
        values = modifiers or []
    aliases = {
        "control": "ctrl",
        "windows": "win",
        "super": "win",
        "command": "cmd",
        "meta": "cmd",
        "option": "alt",
    }
    return [aliases.get(str(value).strip().lower(), str(value).strip().lower()) for value in values if str(value).strip()]


def computer_use(
    action: str,
    app: str = "",
    pid: int = 0,
    window_id: int = 0,
    element: int = 0,
    element_token: str = "",
    snapshot_id: str = "",
    x: int = -1,
    y: int = -1,
    text: str = "",
    key: str = "",
    modifiers: list[str] = None,
    direction: str = "down",
    amount: int = 3,
    max_elements: int = 200,
    query: str = "",
    approval_token: str = "",
) -> str:
    """Inspect or control desktop apps through the installed cua-driver.

    Args:
        action: One of list_apps, list_windows, capture, click, double_click, right_click, type, key, scroll, focus, or bring_to_front.
        app: App name or window-title fragment used to select a target.
        pid: Optional target process id from list_apps/list_windows.
        window_id: Optional target window id from list_windows.
        element: Preferred numbered element from the latest capture of this window.
        element_token: Preferred opaque element handle from capture (safer than an index).
        snapshot_id: Snapshot handle required by current cua-driver when using element.
        x: Fallback window-local x coordinate when no element exists.
        y: Fallback window-local y coordinate when no element exists.
        text: Text for the type action.
        key: Key name for the key action (return, tab, escape, arrows, letters, etc.).
        modifiers: Optional ctrl/control, shift, alt/option, win/super, or cmd/command modifiers.
        direction: Scroll direction: up, down, left, or right.
        amount: Scroll amount (1-50).
        max_elements: Maximum accessibility elements returned by capture (1-1000).
        query: Optional capture filter for accessibility-tree text.
        approval_token: Token supplied only after the user replies "approve TOKEN".

    Returns:
        Accessibility/UI state, action result, or a user approval request.
    """
    selected = action.strip().lower()
    if selected not in _READ_ONLY_ACTIONS | _INPUT_ACTIONS:
        return _failure(
            "unsupported_action",
            "Use list_apps, list_windows, capture, click, double_click, right_click, type, key, scroll, focus, or bring_to_front.",
        )
    if os.getenv("KARA_CUA_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return _failure("computer_use_disabled", "Computer use is disabled by KARA_CUA_ENABLED=0.")

    normalized_modifiers = _normalized_modifiers(modifiers)
    if selected in {"click", "double_click", "right_click"} and not element and not element_token.strip() and not (x >= 0 and y >= 0):
        return _failure("invalid_arguments", "Click actions require element_token, element, or both x and y.")
    if element and not element_token.strip() and not snapshot_id.strip():
        return _failure(
            "invalid_arguments",
            "A bare element index requires the matching snapshot_id from the latest capture; prefer element_token.",
        )
    if selected == "type" and not text:
        return _failure("invalid_arguments", "The type action requires non-empty text.")
    if selected == "key" and not key.strip():
        return _failure("invalid_arguments", "The key action requires a key name.")
    if selected == "scroll" and direction not in {"up", "down", "left", "right"}:
        return _failure("invalid_arguments", "Scroll direction must be up, down, left, or right.")

    try:
        if selected == "list_apps":
            return _compact_apps()
        if selected == "list_windows":
            return _compact_windows()

        approved_target = (
            _pending_target_snapshot(approval_token)
            if selected != "capture" and approval_token.strip()
            else None
        )
        if selected != "capture" and approval_token.strip() and approved_target is None:
            return _failure(
                "approval_invalid",
                "The approval token is unknown, expired, or belongs to another session.",
            )
        if approved_target is not None:
            # Never re-resolve an app name after approval; execute only against the
            # stored window identity, which is revalidated immediately before input.
            target = approved_target
        else:
            try:
                target = _resolve_target(app, int(pid or 0), int(window_id or 0))
            except ValueError as exc:
                return _failure("target_not_found", str(exc))

        intent = {
            "app": app,
            "pid": int(pid or 0),
            "window_id": int(window_id or 0),
            "element": int(element or 0),
            "element_token": element_token.strip(),
            "snapshot_id": snapshot_id.strip(),
            "x": int(x),
            "y": int(y),
            "text": text,
            "key": key,
            "modifiers": normalized_modifiers,
            "direction": direction,
            "amount": int(amount),
            "max_elements": int(max_elements),
            "query": query,
        }

        if selected != "capture":
            if selected == "type":
                summary = f"type {len(text)} characters into '{target['title'] or target['app_name']}'"
            else:
                summary = f"perform computer action '{selected}' on '{target['title'] or target['app_name']}'"
            approval = _approval_gate(selected, intent, target, approval_token, summary)
            if approval:
                return approval

        # Driver input schemas accept pid/window_id, not descriptive identity fields.
        base = {
            "pid": int(target["pid"]),
            "window_id": int(target["window_id"]),
            "session": _SESSION_ID,
        }

        if selected == "capture":
            args: dict[str, Any] = {
                **base,
                "include_screenshot": False,
                "max_elements": max(1, min(int(max_elements or 200), 1000)),
            }
            if query.strip():
                args["query"] = query.strip()
            data = _call_driver("get_window_state", args, timeout=45.0)
            cleaned = _trimmed(data)
            return _json_result(
                {
                    "ok": True,
                    "status": "completed",
                    "action": "capture",
                    "action_sent": False,
                    "target": _target_identity(target),
                    "state": cleaned,
                }
            )

        bound = _verify_bound_target(target)
        if not bound.get("ok"):
            return _failure(
                str(bound.get("code")),
                str(bound.get("message")),
                target=target,
                details=bound,
            )

        if selected in {"focus", "bring_to_front"}:
            foreground = _ensure_foreground(target)
            if not foreground.get("ok"):
                return _failure(
                    str(foreground.get("code") or "foreground_activation_failed"),
                    str(foreground.get("message") or "Could not verify the target foreground window."),
                    target=target,
                    details=foreground,
                )
            return _success(selected, target, foreground=foreground)

        driver_tool = selected
        args = dict(base)
        keyboard = False
        if selected in {"click", "double_click", "right_click"}:
            driver_tool = "click"
            args["count"] = 2 if selected == "double_click" else 1
            args["button"] = "right" if selected == "right_click" else "left"
            if element_token.strip():
                args["element_token"] = element_token.strip()
            elif element:
                args["element_index"] = int(element)
                if snapshot_id.strip():
                    args["snapshot_id"] = snapshot_id.strip()
            else:
                args.update({"x": int(x), "y": int(y)})
            if normalized_modifiers:
                args["modifier"] = normalized_modifiers
        elif selected == "type":
            driver_tool = "type_text"
            keyboard = True
            args["text"] = text
            if element_token.strip():
                args["element_token"] = element_token.strip()
            elif element:
                args["element_index"] = int(element)
                if snapshot_id.strip():
                    args["snapshot_id"] = snapshot_id.strip()
            elif x >= 0 and y >= 0:
                args.update({"x": int(x), "y": int(y)})
        elif selected == "key":
            keyboard = True
            if normalized_modifiers:
                driver_tool = "hotkey"
                args["keys"] = [*normalized_modifiers, key.strip().lower()]
            else:
                driver_tool = "press_key"
                args["key"] = key.strip().lower()
            if x >= 0 and y >= 0:
                args.update({"x": int(x), "y": int(y)})
        elif selected == "scroll":
            driver_tool = "scroll"
            args.update({"direction": direction, "amount": max(1, min(int(amount), 50))})

        result, foreground = _execute_input_action(
            driver_tool, args, target, keyboard=keyboard
        )
        if result is None and foreground is not None:
            return _failure(
                str(foreground.get("code") or "foreground_activation_failed"),
                str(foreground.get("message") or "Could not verify the target foreground window."),
                target=target,
                details=foreground,
            )
        return _success(selected, target, driver_result=result, foreground=foreground)
    except DriverCallError as exc:
        return _failure(
            "driver_action_failed",
            str(exc),
            target=locals().get("target"),
            details=exc.payload,
            action_sent=None,
        )
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        return _failure(
            "computer_use_failed",
            str(exc),
            target=locals().get("target"),
            action_sent=None,
        )

# --- Registry declaration ------------------------------------------------------
# Consumed by tools.registry; this is the single source of truth for which
# functions in this module are exposed to the model and which of them are safe
# for unattended scheduled runs.
TOOL_GROUP = "computer"

TOOLS = [
    computer_use,
]

SCHEDULED_SAFE: set[str] = set()

# Tools with no side effects. Used to decide what may run concurrently; a
# superset of SCHEDULED_SAFE, which is a separate policy about unattended runs.
READ_ONLY: set[str] = set()
