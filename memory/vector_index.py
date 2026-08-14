"""Lightweight local vector index over Kara's learnings + session summaries.

Design goals (per the chosen "we fully control it" approach):
  * No heavy dependencies - pure-Python cosine similarity, JSON on disk.
  * Incremental - only re-embed records whose content hash changed.
  * Hybrid - blend semantic similarity with keyword overlap so exact terms
    (names, IDs, error codes) still rank, while concepts/paraphrases also match.
  * Degrades gracefully - if Ollama is down, fall back to keyword-only search.

Two sources feed the index, both curated rather than verbatim:
  * ``brain/learnings/*.md`` - durable facts Kara chose to save.
  * ``session_summaries`` rows in SQLite - one recap per finished conversation.

Raw transcripts are deliberately *not* indexed. They live in the ``messages``
table for replay; embedding them made recall compete decisions against chatter.

The index is stored at ``brain/index/index.json`` as:
    {
      "version": 2,
      "model": "nomic-embed-text",
      "records": { "<key>": {"hash": "...", "chunk_ids": [...]} },
      "chunks": [ {"id", "source", "title", "text", "vector"} ]
    }

STUDY GUIDE
-----------
* Chunks memory records, embeds them via Ollama, stores vectors in JSON on disk.
* Hybrid search combines cosine similarity with keyword overlap scoring.
* Key concepts: hashlib, dataclasses, zip(), lambda sort keys, incremental updates.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

import config
from memory import embeddings
from memory import session_db

# Bump when the on-disk index layout changes so stale indexes rebuild instead of
# being misread.
INDEX_VERSION = 2

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150

# LEARN: Module-level cache — keeps the loaded index in memory between searches
# so we don't re-read the (potentially large) JSON file on every tool call.
_index_cache: dict | None = None
_index_cache_key: tuple | None = None


@dataclass(frozen=True)
class MemoryRecord:
    """One indexable unit of memory, whatever it was stored in."""

    key: str  # stable identity, e.g. "learnings/2026-01-01-foo.md" or "summary:12"
    title: str
    text: str


def _memory_fingerprint() -> tuple:
    """Cheap change detector across both sources.

    For learnings it is (path, mtime, size) — stat() is far cheaper than reading
    and hashing. For summaries it is the row count plus the highest id and
    timestamp, since rows are only ever appended.
    """
    entries: list[tuple] = []
    for path in sorted(config.LEARNINGS_DIR.glob("*.md")):
        try:
            st = path.stat()
            entries.append((_learning_key(path), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    try:
        entries.append(("session_summaries",) + session_db.session_summary_fingerprint())
    except Exception:
        pass
    return tuple(entries)


def _load_index() -> dict:
    if config.INDEX_FILE.exists():
        try:
            return json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _empty_index()


def _empty_index() -> dict:
    return {
        "version": INDEX_VERSION,
        "model": config.EMBED_MODEL,
        "records": {},
        "chunks": [],
    }


def _save_index(index: dict) -> None:
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    config.INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )


def _learning_key(path) -> str:
    return f"learnings/{path.name}"


def _sources() -> list[MemoryRecord]:
    """Every record that participates in semantic memory."""
    records: list[MemoryRecord] = []

    for path in sorted(config.LEARNINGS_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        records.append(
            MemoryRecord(_learning_key(path), _title_of(text, path.stem), text)
        )

    try:
        summaries = session_db.load_session_summaries()
    except Exception:
        summaries = []
    for row in summaries:
        records.append(
            MemoryRecord(
                f"summary:{row['id']}",
                row["title"] or row["session_key"],
                row["summary"],
            )
        )

    return records


def _hash(text: str) -> str:
    # LEARN: SHA-256 hex digest detects file content changes without re-reading old hash files.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    # LEARN: Sliding window with overlap — each chunk shares CHUNK_OVERLAP chars with the previous.
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _cosine(a: list[float], b: list[float]) -> float:
    # LEARN: zip() pairs elements from two lists; generator expressions inside sum() compute dot product.
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _keyword_score(query: str, text: str) -> float:
    # LEARN: Sets of words enable fast intersection (q & t) for keyword overlap ratio.
    q = set(_WORD_RE.findall(query.lower()))
    if not q:
        return 0.0
    t = set(_WORD_RE.findall(text.lower()))
    return len(q & t) / len(q)


def reindex() -> dict:
    """Bring the index up to date, re-embedding only changed/new records.

    Returns a small status dict. Requires Ollama; raises embeddings.EmbeddingError
    if unavailable (callers may catch and fall back to keyword search).
    """
    index = _load_index()

    if (
        index.get("model") != config.EMBED_MODEL
        or index.get("version") != INDEX_VERSION
    ):
        index = _empty_index()

    # LEARN: Dict comprehension maps record key → record for quick lookup.
    current = {record.key: record for record in _sources()}
    records_meta: dict = index.get("records", {})
    chunks: list = index.get("chunks", [])

    removed = [key for key in records_meta if key not in current]
    changed = [
        (record, _hash(record.text))
        for record in current.values()
        if records_meta.get(record.key, {}).get("hash") != _hash(record.text)
    ]

    if not changed and not removed:
        return {"status": "up-to-date", "records": len(current), "chunks": len(chunks)}

    stale = set(removed) | {record.key for record, _ in changed}
    chunks = [c for c in chunks if c["source"] not in stale]
    for key in removed:
        records_meta.pop(key, None)

    next_id = (max((c["id"] for c in chunks), default=-1)) + 1
    for record, digest in changed:
        pieces = _chunk(record.text)
        vectors = embeddings.embed_batch(pieces)
        chunk_ids = []
        for piece, vec in zip(pieces, vectors):
            chunks.append(
                {
                    "id": next_id,
                    "source": record.key,
                    "title": record.title,
                    "text": piece,
                    "vector": vec,
                }
            )
            chunk_ids.append(next_id)
            next_id += 1
        records_meta[record.key] = {"hash": digest, "chunk_ids": chunk_ids}

    index["version"] = INDEX_VERSION
    index["records"] = records_meta
    index["chunks"] = chunks
    _save_index(index)
    _cache_index(index)
    return {
        "status": "updated",
        "reembedded_records": len(changed),
        "removed_records": len(removed),
        "chunks": len(chunks),
    }


def _cache_index(index: dict) -> None:
    global _index_cache, _index_cache_key
    _index_cache = index
    _index_cache_key = _memory_fingerprint()


def _keyword_only_search(query: str, top_k: int) -> list[dict]:
    results = []
    for record in _sources():
        score = _keyword_score(query, record.text)
        if score > 0:
            results.append(
                {
                    "source": record.key,
                    "title": record.title,
                    "text": record.text[:400],
                    "score": score,
                }
            )
    # LEARN: sort(key=lambda ...) orders by score descending; lambda is an inline anonymous function.
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


def search(query: str, top_k: int = 5, semantic_weight: float = 0.7) -> dict:
    """Hybrid search over learnings + sessions.

    Returns {"mode": "hybrid"|"keyword", "results": [...], "note": str}.
    Falls back to keyword-only if Ollama is unavailable.
    """
    if not _sources():
        return {
            "mode": "empty",
            "results": [],
            "note": "No learnings or session summaries yet.",
        }

    try:
        # LEARN: Only pay the reindex + reload cost when memory files actually
        # changed since the last search (compared via cheap stat() fingerprint).
        if _index_cache is None or _memory_fingerprint() != _index_cache_key:
            status = reindex()
            if status.get("status") != "updated":
                # Nothing re-embedded — cache the on-disk index and new fingerprint.
                _cache_index(_load_index())
        qvec = embeddings.embed(query)
    except embeddings.EmbeddingError as e:
        return {
            "mode": "keyword",
            "results": _keyword_only_search(query, top_k),
            "note": f"Semantic search unavailable ({e}). Fell back to keyword search.",
        }

    index = _index_cache or _load_index()
    scored = []
    for c in index["chunks"]:
        sem = _cosine(qvec, c["vector"])
        kw = _keyword_score(query, c["text"])
        combined = semantic_weight * sem + (1 - semantic_weight) * kw
        # LEARN: {**c, "score": combined} spreads dict c and adds new keys (dict unpacking merge).
        scored.append({**c, "score": combined, "semantic": sem, "keyword": kw})

    scored.sort(key=lambda r: r["score"], reverse=True)
    top = [
        {"source": c["source"], "title": c["title"], "text": c["text"], "score": round(c["score"], 3)}
        for c in scored[:top_k]
    ]
    return {"mode": "hybrid", "results": top, "note": ""}
