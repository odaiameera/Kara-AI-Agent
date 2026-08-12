"""Read-only Windows system inspection tools for Kara.

Every public function in this module is observational: it can inventory Windows
state but cannot stop processes, change services, edit scheduled tasks, or alter
disks. PowerShell receives fixed scripts only; user filters are applied in Python
so model-provided text is never interpolated into a shell command.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

_MAX_OUTPUT_CHARS = 2_000_000


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"ok": False, "error": message})


def _powershell_command() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


def _run_powershell_json(script: str, timeout: float = 20.0) -> Any:
    """Run one fixed read-only PowerShell script and decode its JSON output."""
    executable = _powershell_command()
    if not executable:
        raise RuntimeError("PowerShell was not found on this Windows machine.")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown PowerShell error").strip()
        raise RuntimeError(detail[:2000])
    output = (completed.stdout or "").strip()
    if not output:
        return []
    if len(output) > _MAX_OUTPUT_CHARS:
        raise RuntimeError("Windows inventory exceeded the safe output limit.")
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PowerShell returned invalid JSON: {exc}") from exc


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def list_processes(query: str = "", sort_by: str = "memory", max_results: int = 25) -> str:
    """List running Windows processes with PID, parent PID, memory use, and executable name.

    Args:
        query: Optional case-insensitive process-name filter. Applied locally, never passed to a shell.
        sort_by: Sort order: memory, pid, or name.
        max_results: Maximum processes returned (1-100).

    Returns:
        Structured read-only process inventory.
    """
    script = r"""
Get-CimInstance Win32_Process |
  Select-Object Name, ProcessId, ParentProcessId, WorkingSetSize, ExecutablePath |
  ConvertTo-Json -Depth 3 -Compress
"""
    selected_sort = sort_by.strip().casefold()
    if selected_sort not in {"memory", "pid", "name"}:
        return _error("sort_by must be memory, pid, or name.")
    limit = max(1, min(int(max_results), 100))
    needle = query.strip().casefold()
    try:
        raw_rows = _as_rows(_run_powershell_json(script))
        processes = [
            {
                "name": str(row.get("Name") or ""),
                "pid": int(row.get("ProcessId") or 0),
                "parent_pid": int(row.get("ParentProcessId") or 0),
                "memory_bytes": int(row.get("WorkingSetSize") or 0),
                "executable_path": str(row.get("ExecutablePath") or ""),
            }
            for row in raw_rows
        ]
        if needle:
            processes = [row for row in processes if needle in row["name"].casefold()]
        sort_key = {
            "memory": lambda row: (row["memory_bytes"], row["pid"]),
            "pid": lambda row: (row["pid"],),
            "name": lambda row: (row["name"].casefold(), row["pid"]),
        }[selected_sort]
        processes.sort(key=sort_key, reverse=selected_sort != "name")
        total = len(processes)
        return _json(
            {
                "ok": True,
                "count": total,
                "returned": min(total, limit),
                "truncated": total > limit,
                "processes": processes[:limit],
            }
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error(f"Could not inspect Windows processes: {exc}")


def list_services(query: str = "", status: str = "all", max_results: int = 50) -> str:
    """List Windows services with state, startup mode, display name, and owning PID.

    Args:
        query: Optional case-insensitive service or display-name filter.
        status: Filter: all, running, stopped, or paused.
        max_results: Maximum services returned (1-200).

    Returns:
        Structured read-only Windows service inventory.
    """
    script = r"""
Get-CimInstance Win32_Service |
  Select-Object Name, DisplayName, State, StartMode, ProcessId |
  ConvertTo-Json -Depth 3 -Compress
"""
    selected_status = status.strip().casefold()
    if selected_status not in {"all", "running", "stopped", "paused"}:
        return _error("status must be all, running, stopped, or paused.")
    limit = max(1, min(int(max_results), 200))
    needle = query.strip().casefold()
    try:
        services = [
            {
                "name": str(row.get("Name") or ""),
                "display_name": str(row.get("DisplayName") or ""),
                "status": str(row.get("State") or ""),
                "start_mode": str(row.get("StartMode") or ""),
                "pid": int(row.get("ProcessId") or 0),
            }
            for row in _as_rows(_run_powershell_json(script))
        ]
        if needle:
            services = [
                row
                for row in services
                if needle in row["name"].casefold() or needle in row["display_name"].casefold()
            ]
        if selected_status != "all":
            services = [row for row in services if row["status"].casefold() == selected_status]
        services.sort(key=lambda row: (row["name"].casefold(), row["pid"]))
        total = len(services)
        return _json(
            {
                "ok": True,
                "count": total,
                "returned": min(total, limit),
                "truncated": total > limit,
                "services": services[:limit],
            }
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error(f"Could not inspect Windows services: {exc}")


def list_scheduled_tasks(query: str = "", state: str = "all", max_results: int = 50) -> str:
    """List Windows scheduled tasks with path, state, principal, action count, and trigger count.

    Args:
        query: Optional case-insensitive task-name or task-path filter.
        state: Filter: all, ready, running, disabled, or queued.
        max_results: Maximum tasks returned (1-200).

    Returns:
        Structured read-only Scheduled Task inventory; command arguments are not exposed.
    """
    script = r"""
Get-ScheduledTask | ForEach-Object {
  [PSCustomObject]@{
    TaskName = $_.TaskName
    TaskPath = $_.TaskPath
    State = [string]$_.State
    Principal = $_.Principal.UserId
    ActionCount = @($_.Actions).Count
    TriggerCount = @($_.Triggers).Count
  }
} | ConvertTo-Json -Depth 3 -Compress
"""
    selected_state = state.strip().casefold()
    if selected_state not in {"all", "ready", "running", "disabled", "queued"}:
        return _error("state must be all, ready, running, disabled, or queued.")
    limit = max(1, min(int(max_results), 200))
    needle = query.strip().casefold()
    try:
        tasks = [
            {
                "name": str(row.get("TaskName") or ""),
                "path": str(row.get("TaskPath") or ""),
                "state": str(row.get("State") or ""),
                "principal": str(row.get("Principal") or ""),
                "action_count": int(row.get("ActionCount") or 0),
                "trigger_count": int(row.get("TriggerCount") or 0),
            }
            for row in _as_rows(_run_powershell_json(script))
        ]
        if needle:
            tasks = [
                row
                for row in tasks
                if needle in row["name"].casefold() or needle in row["path"].casefold()
            ]
        if selected_state != "all":
            tasks = [row for row in tasks if row["state"].casefold() == selected_state]
        tasks.sort(key=lambda row: (row["path"].casefold(), row["name"].casefold()))
        total = len(tasks)
        return _json(
            {
                "ok": True,
                "count": total,
                "returned": min(total, limit),
                "truncated": total > limit,
                "tasks": tasks[:limit],
            }
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error(f"Could not inspect Windows scheduled tasks: {exc}")


def disk_usage() -> str:
    """Inspect fixed Windows disks, labels, filesystems, capacity, used space, and free space.

    Returns:
        Structured read-only disk usage for local fixed drives.
    """
    script = r"""
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
  Select-Object DeviceID, VolumeName, FileSystem, Size, FreeSpace |
  ConvertTo-Json -Depth 3 -Compress
"""
    try:
        disks: list[dict[str, Any]] = []
        for row in _as_rows(_run_powershell_json(script)):
            size = max(0, int(row.get("Size") or 0))
            free = max(0, int(row.get("FreeSpace") or 0))
            used = max(0, size - free)
            disks.append(
                {
                    "drive": str(row.get("DeviceID") or ""),
                    "label": str(row.get("VolumeName") or ""),
                    "filesystem": str(row.get("FileSystem") or ""),
                    "size_bytes": size,
                    "used_bytes": used,
                    "free_bytes": free,
                    "free_percent": round((free / size * 100) if size else 0.0, 2),
                }
            )
        disks.sort(key=lambda row: row["drive"].casefold())
        return _json({"ok": True, "count": len(disks), "disks": disks})
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error(f"Could not inspect Windows disks: {exc}")


def system_overview() -> str:
    """Inspect Windows edition, version, hardware model, memory, CPU count, and boot time.

    Returns:
        Structured read-only system inventory. Environment variables and credentials are excluded.
    """
    script = r"""
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
[PSCustomObject]@{
  ComputerName = $env:COMPUTERNAME
  OsName = $os.Caption
  OsVersion = $os.Version
  LastBootUpTime = $os.LastBootUpTime
  Manufacturer = $cs.Manufacturer
  Model = $cs.Model
  TotalPhysicalMemory = [Int64]$cs.TotalPhysicalMemory
  LogicalProcessors = [Int32]$cs.NumberOfLogicalProcessors
} | ConvertTo-Json -Compress
"""
    try:
        raw = _run_powershell_json(script)
        system = {
            "computer_name": str(raw.get("ComputerName") or ""),
            "os_name": str(raw.get("OsName") or ""),
            "os_version": str(raw.get("OsVersion") or ""),
            "last_boot_time": str(raw.get("LastBootUpTime") or ""),
            "manufacturer": str(raw.get("Manufacturer") or ""),
            "model": str(raw.get("Model") or ""),
            "memory_bytes": int(raw.get("TotalPhysicalMemory") or 0),
            "logical_processors": int(raw.get("LogicalProcessors") or 0),
        }
        return _json({"ok": True, "system": system})
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error(f"Could not inspect Windows system information: {exc}")

# --- Registry declaration ------------------------------------------------------
# Consumed by tools.registry; this is the single source of truth for which
# functions in this module are exposed to the model and which of them are safe
# for unattended scheduled runs.
TOOL_GROUP = "windows"

TOOLS = [
    system_overview,
    list_processes,
    list_services,
    list_scheduled_tasks,
    disk_usage,
]

SCHEDULED_SAFE = {
    "system_overview",
    "list_processes",
    "list_services",
    "list_scheduled_tasks",
    "disk_usage",
}

# Tools with no side effects. Used to decide what may run concurrently; a
# superset of SCHEDULED_SAFE, which is a separate policy about unattended runs.
READ_ONLY = {
    "system_overview",
    "list_processes",
    "list_services",
    "list_scheduled_tasks",
    "disk_usage",
}
