"""Local file search (RAG) with qwen3-embed-8b.

Indexing and search both need the embedding model on the GPU, so they run as queue jobs through
the single worker (kinds "index" and "search"): the worker loads qwen3-embed-8b, embeds, and
stores/queries vectors in ~/.law/state/rag.db. No cloud, no API keys.

Vectors are stored as packed float32 bytes; similarity is cosine, computed in Python (fine for
the thousands-of-chunks scale a personal workstation indexes).
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import time
import urllib.request
from pathlib import Path

EMBED_MODEL = "qwen3-embed-8b"
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
            ".html", ".css", ".sh", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".rb", ".php",
            ".sql", ".ini", ".cfg", ".conf", ".tex", ".csv", ".log"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", "build",
             ".local-agent", ".cache", "target", ".idea"}
MAX_FILE_BYTES = 400_000


def _db(paths) -> sqlite3.Connection:
    p = paths.state / "rag.db"
    c = sqlite3.connect(p, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS chunks(project TEXT, path TEXT, idx INTEGER, text TEXT, "
              "dim INTEGER, vec BLOB, ts REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS chunks_project ON chunks(project)")
    return c


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unpack(b: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", b))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def embed(base_url: str, served_name: str, texts: list[str], timeout: float = 120) -> list[list[float]]:
    body = json.dumps({"model": served_name, "input": texts}).encode()
    req = urllib.request.Request(base_url + "/v1/embeddings", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return [item["embedding"] for item in d["data"]]


def chunk_text(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK_CHARS])
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix.lower() in TEXT_EXT:
            try:
                if p.stat().st_size <= MAX_FILE_BYTES:
                    yield p
            except OSError:
                continue


def index(paths, base_url: str, served_name: str, root: str | Path, *, touch=None, check=None,
          on_progress=None) -> dict:
    """Embed every text file under `root` and store the vectors. Replaces any previous index for
    this folder. Returns {files, chunks}."""
    root = Path(root).expanduser().resolve()
    key = str(root)
    c = _db(paths)
    c.execute("DELETE FROM chunks WHERE project=?", (key,))
    files = list(iter_files(root))
    n_chunks = 0
    for fi, f in enumerate(files):
        if check:
            check()
        if touch:
            touch()
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        chunks = chunk_text(text)
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        for start in range(0, len(chunks), 16):           # batch of 16 per embed call
            batch = chunks[start:start + 16]
            vecs = embed(base_url, served_name, batch)
            for j, (ch, v) in enumerate(zip(batch, vecs)):
                c.execute("INSERT INTO chunks(project,path,idx,text,dim,vec,ts) VALUES(?,?,?,?,?,?,?)",
                          (key, rel, start + j, ch, len(v), _pack(v), time.time()))
                n_chunks += 1
            if touch:
                touch()
        if on_progress:
            on_progress(fi + 1, len(files), rel)
    c.commit()
    c.close()
    return {"files": len(files), "chunks": n_chunks, "root": key}


def search(paths, base_url: str, served_name: str, root: str | Path, query: str, k: int = 8) -> list[dict]:
    root = str(Path(root).expanduser().resolve())
    qv = embed(base_url, served_name, [query])[0]
    c = _db(paths)
    rows = c.execute("SELECT path, idx, text, dim, vec FROM chunks WHERE project=?", (root,)).fetchall()
    c.close()
    scored = []
    for path, idx, text, dim, vec in rows:
        scored.append((_cosine(qv, _unpack(vec, dim)), path, idx, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"score": round(s, 3), "path": p, "chunk": i, "text": t[:600]} for s, p, i, t in scored[:k]]


def status(paths) -> list[dict]:
    try:
        c = _db(paths)
        rows = c.execute("SELECT project, COUNT(*), COUNT(DISTINCT path) FROM chunks GROUP BY project").fetchall()
        c.close()
        return [{"root": r[0], "chunks": r[1], "files": r[2]} for r in rows]
    except sqlite3.Error:
        return []
