"""Install or remove Kara gateway Windows logon startup (no terminal window).

STUDY GUIDE
-----------
* Registers Kara to start automatically when Windows logs in (via PowerShell scripts).
* Validates platform and virtualenv before running installer scripts.
* Key concepts: ``subprocess.run``, ``sys.platform``, ``sys.exit`` for CLI errors.
"""
from __future__ import annotations

import subprocess
import sys

import config

PACKAGE_DIR = config.PACKAGE_DIR


def _run_ps(script_name: str) -> None:
    # LEARN: subprocess.run launches an external program; check=True raises if it exits non-zero.
    # The ternary picks different argument lists for install vs uninstall scripts.
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
    # LEARN: sys.platform is "win32" on Windows; sys.exit(1) stops the program with an error code.
    if sys.platform != "win32":
        print("This installer is for Windows only.")
        sys.exit(1)
    pythonw = PACKAGE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        print(f"Missing {pythonw}. Run: cd personal_agent && uv sync")
        sys.exit(1)
    _run_ps("install_gateway.ps1")
    print("Done. Kara starts at Windows logon (fully hidden, no console).")
    print("Start now:  schtasks /Run /TN KaraGateway")
    print("Stop:       powershell -File scripts/stop_gateway.ps1")


def uninstall() -> None:
    _run_ps("uninstall_gateway.ps1")


def main() -> None:
    # LEARN: ``"--uninstall" in sys.argv`` checks if the flag appears anywhere in command-line args.
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()
