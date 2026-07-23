"""Lightweight local vector index over Kara's learnings + sessions.

Design goals (per the chosen "we fully control it" approach):
  * No heavy dependencies - pure-Python cosine similarity, JSON on disk.
  * Incremental - only re-embed files whose content hash changed.
  * Hybrid - blend semantic similarity with keyword overlap so exact terms
    (names, IDs, error codes) still rank, while concepts/paraphrases also match.
  * Degrades gracefully - if Ollama is down, fall back to keyword-only search.

The index is stored at ``brain/index/index.json`` as:
    {
      "model": "nomic-embed-text",
      "files": { "<relpath>": {"hash": "...", "chunk_ids": [...]} },
      "chunks": [ {"id", "source", "title", "text", "vector"} ]
    }

STUDY GUIDE
-----------
* Chunks markdown files, embeds them via Ollama, stores vectors in JSON on disk.
* Hybrid search combines cosine similarity with keyword overlap scoring.
* Key concepts: hashlib, list comprehensions, zip(), lambda sort keys, incremental updates.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import config
import embeddings

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150

# LEARN: Module-level cache — keeps the loaded index in memory between searches
# so we don't re-read the (potentially large) JSON file on every tool call.
_index_cache: dict | None = None
_index_cache_key: tuple | None = None


def _memory_fingerprint() -> tuple:
    """Cheap change detector: (path, mtime, size) for every memory file.

    stat() is much faster than reading + hashing file contents, so search()
    can skip the full reindex when nothing on disk has changed.
    """
    entries = []
    for path in _sources():
        try:
            st = path.stat()
            entries.append((_rel(path), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(entries)


def _load_index() -> dict:
    if config.INDEX_FILE.exists():
        try:
            return json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"model": config.EMBED_MODEL, "files": {}, "chunks": []}


def _save_index(index: dict) -> None:
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    config.INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )


def _sources() -> list[Path]:
    """All markdown files that participate in semantic memory."""
    return sorted(config.LEARNINGS_DIR.glob("*.md")) + sorted(
        config.SESSIONS_DIR.glob("*.md")
    )


def _rel(path: Path) -> str:
    return str(path.relative_to(config.BRAIN_DIR)).replace("\\", "/")


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
    """Bring the index up to date, re-embedding only changed/new files.

    Returns a small status dict. Requires Ollama; raises embeddings.EmbeddingError
    if unavailable (callers may catch and fall back to keyword search).
    """
    index = _load_index()

    if index.get("model") != config.EMBED_MODEL:
        index = {"model": config.EMBED_MODEL, "files": {}, "chunks": []}

    # LEARN: Dict comprehension maps relative path string → Path for quick lookup.
    current = {_rel(p): p for p in _sources()}
    files_meta: dict = index.get("files", {})
    chunks: list = index.get("chunks", [])

    changed, removed = [], []
    for rel in list(files_meta.keys()):
        if rel not in current:
            removed.append(rel)
    for rel, path in current.items():
        text = path.read_text(encoding="utf-8")
        h = _hash(text)
        if files_meta.get(rel, {}).get("hash") != h:
            changed.append((rel, path, text, h))

    if not changed and not removed:
        return {"status": "up-to-date", "files": len(current), "chunks": len(chunks)}

    stale = set(removed) | {rel for rel, *_ in changed}
    chunks = [c for c in chunks if c["source"] not in stale]
    for rel in removed:
        files_meta.pop(rel, None)

    next_id = (max((c["id"] for c in chunks), default=-1)) + 1
    for rel, path, text, h in changed:
        title = _title_of(text, path.stem)
        pieces = _chunk(text)
        vectors = embeddings.embed_batch(pieces)
        chunk_ids = []
        for piece, vec in zip(pieces, vectors):
            chunks.append(
                {"id": next_id, "source": rel, "title": title, "text": piece, "vector": vec}
            )
            chunk_ids.append(next_id)
            next_id += 1
        files_meta[rel] = {"hash": h, "chunk_ids": chunk_ids}

    index["files"] = files_meta
    index["chunks"] = chunks
    _save_index(index)
    _cache_index(index)
    return {
        "status": "updated",
        "reembedded_files": len(changed),
        "removed_files": len(removed),
        "chunks": len(chunks),
    }


def _cache_index(index: dict) -> None:
    global _index_cache, _index_cache_key
    _index_cache = index
    _index_cache_key = _memory_fingerprint()


def _keyword_only_search(query: str, top_k: int) -> list[dict]:
    results = []
    for path in _sources():
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        score = _keyword_score(query, text)
        if score > 0:
            results.append(
                {"source": _rel(path), "title": _title_of(text, path.stem),
                 "text": text[:400], "score": score}
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
        return {"mode": "empty", "results": [], "note": "No learnings or sessions yet."}

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
