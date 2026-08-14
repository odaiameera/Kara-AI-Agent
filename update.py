"""Apply updates and restart the gateway.

STUDY GUIDE
-----------
* Pulls latest git changes, syncs Python deps with ``uv``, then signals gateway restart.
* Runs external commands via subprocess with printed command lines for transparency.
* Key concepts: ``subprocess.CalledProcessError``, optional tool fallback, restart flags.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import config
from gateway import restart as gw_restart

PACKAGE_DIR = config.PACKAGE_DIR
REPO_ROOT = config.REPO_ROOT


def _run(cmd: list[str], cwd: Path) -> None:
    print(f"> {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main() -> None:
    print("Updating Kara...")

    # LEARN: Only git pull if this folder is a git repo; continue on failure so local updates still work.
    if (REPO_ROOT / ".git").exists():
        try:
            _run(["git", "pull", "--ff-only"], cwd=REPO_ROOT)
        except subprocess.CalledProcessError:
            print("Warning: git pull failed — continuing with local code.")

    try:
        _run(["uv", "sync"], cwd=PACKAGE_DIR)
    except FileNotFoundError:
        print("uv not found — skip dependency sync.")
    except subprocess.CalledProcessError:
        print("Warning: uv sync failed.")

    # LEARN: Writes a flag file the running gateway watches — triggers self-restart without killing manually.
    gw_restart.request_restart("kara update")
    print("Update complete. Gateway will restart automatically if it is running.")
    print("If not running, start with: uv run python -m gateway.run")


if __name__ == "__main__":
    main()
