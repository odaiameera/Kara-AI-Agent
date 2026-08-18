"""Install or remove the Kara gateway so it starts at user login."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import config

PACKAGE_DIR = config.PACKAGE_DIR
LINUX_UNIT_NAME = "kara-gateway.service"


def render_linux_unit(repo: Path, python: Path) -> str:
    """Fill a systemd user unit with this checkout's paths at install time."""
    return (
        "[Unit]\n"
        "Description=Kara Telegram gateway\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={repo}\n"
        f'ExecStart="{python}" -m gateway.run\n'
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _linux_unit_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_home / "systemd" / "user" / LINUX_UNIT_NAME


def _systemctl(args: list[str]) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def _linux_python() -> Path:
    return PACKAGE_DIR / ".venv" / "bin" / "python"


def _install_windows() -> None:
    pythonw = PACKAGE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        print(f"Missing {pythonw}. Run 'uv sync' in {PACKAGE_DIR} first.")
        sys.exit(1)
    _run_ps("install_gateway.ps1")
    print("Done. Kara starts at Windows logon (fully hidden, no console).")
    print("Start now:  schtasks /Run /TN KaraGateway")
    print("Stop:       powershell -File scripts/stop_gateway.ps1")


def _install_linux() -> None:
    python = _linux_python()
    if not python.exists():
        print(f"Missing {python}. Run 'uv sync' in {PACKAGE_DIR} first.")
        sys.exit(1)
    unit = _linux_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(render_linux_unit(PACKAGE_DIR, python), encoding="utf-8")
    _systemctl(["daemon-reload"])
    _systemctl(["enable", "--now", LINUX_UNIT_NAME])
    print(f"Installed {unit}")
    print("Kara starts at login via the systemd user unit.")
    print("Start now:  systemctl --user start kara-gateway.service")
    print("Stop:       systemctl --user stop kara-gateway.service")
    print("Logs:       journalctl --user -u kara-gateway.service -f")
    print(f"Also:       {PACKAGE_DIR / 'brain' / 'logs'}")


def _uninstall_windows() -> None:
    _run_ps("uninstall_gateway.ps1")


def _uninstall_linux() -> None:
    unit = _linux_unit_path()
    try:
        _systemctl(["disable", "--now", LINUX_UNIT_NAME])
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    if unit.exists():
        unit.unlink()
    try:
        _systemctl(["daemon-reload"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    print("Removed the Kara systemd user unit.")


def _run_ps(script_name: str) -> None:
    script = PACKAGE_DIR / "scripts" / script_name
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PackageDir",
            str(PACKAGE_DIR),
        ]
        if "install" in script_name
        else [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        check=True,
    )


def install() -> None:
    if sys.platform == "win32":
        _install_windows()
        return
    if sys.platform.startswith("linux"):
        _install_linux()
        return
    print("Gateway auto-start is not implemented on this platform yet.")
    sys.exit(1)


def uninstall() -> None:
    if sys.platform == "win32":
        _uninstall_windows()
        return
    if sys.platform.startswith("linux"):
        _uninstall_linux()
        return
    print("Gateway auto-start is not implemented on this platform yet.")
    sys.exit(1)


def main() -> None:
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()
