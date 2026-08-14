"""Kara's memory: the local brain, conversation store, and semantic index.

Core memory and learnings are Markdown on disk (``memory.store``); transcripts,
session summaries, and per-turn usage live in SQLite (``memory.session_db``).
Recall is built from learnings plus summaries (``memory.vector_index``), and
``memory.context_budget`` keeps a session inside the model's window.
"""
