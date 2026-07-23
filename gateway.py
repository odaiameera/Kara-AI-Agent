"""Entry point for the Kara gateway daemon.

Prefer: ``uv run python -m gateway.run`` (avoids clashing with the ``gateway/`` package).

STUDY GUIDE
-----------
* Thin wrapper that starts the long-running Telegram gateway process.
* Re-exports ``gateway.run.main`` so you can run ``uv run gateway.py``.
* Key concepts: package re-exports, ``if __name__ == "__main__"`` entry points.
"""
from gateway.run import main

# LEARN: ``if __name__ == "__main__"`` runs main() only when this file is executed directly.
if __name__ == "__main__":
    main()
