"""Backward-compatible Telegram entry — prefer ``uv run gateway.py``.

STUDY GUIDE
-----------
* Legacy alias kept so old scripts/docs that call telegram_bot.py still work.
* Delegates entirely to ``gateway.run.main`` — no duplicate Telegram logic here.
* Key concepts: backward compatibility, import-and-delegate pattern.
"""
from gateway.run import main

# LEARN: Thin entry point — imports and calls the same gateway main as gateway.py.
if __name__ == "__main__":
    main()
