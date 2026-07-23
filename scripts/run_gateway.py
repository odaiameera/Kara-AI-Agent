"""Hidden gateway launcher for Windows (logs boot failures to brain/logs/)."""
from __future__ import annotations

import os
import runpy
import sys
import traceback
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
_BOOT_LOG = _PKG_ROOT / "brain" / "logs" / "gateway_boot.log"


def _log(text: str) -> None:
    _BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _BOOT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


if __name__ == "__main__":
    if _BOOT_LOG.exists():
        _BOOT_LOG.unlink()
    _log("boot: starting")
    os.chdir(_PKG_ROOT)
    if str(_PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(_PKG_ROOT))
    try:
        # LEARN: ``-m gateway.run`` avoids the top-level gateway.py file shadowing the package.
        _log("boot: launching gateway.run")
        runpy.run_module("gateway.run", run_name="__main__")
    except SystemExit as exc:
        _log(f"boot: SystemExit {exc.code}")
        if exc.code not in (0, None):
            raise
    except Exception:
        _log("boot: crash\n" + traceback.format_exc())
        raise
