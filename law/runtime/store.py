"""Central per-project agent store (SQLite, WAL) at ~/.law/state/agent.db.

Everything an agent run used to scatter into <project>/.local-agent/ now lives here,
keyed by the project's absolute path, so project folders stay clean:

  specs      the request-translator's task spec, one per job
  handoffs   the brief handed to each delegated sub-job
  runs       every raw model reply (step-<n> / translate-<n>), kept for debugging
  memory     durable facts the `remember` tool saved (read with `law agent --show-memory`)

The ONE thing that stays a file is <project>/.local-agent/AGENT.md: optional project
instructions you write (and can commit). It is read if present and never created for you.
Undo snapshots keep their own git dir under ~/.law/state/undo/ (see undo.py).

Legacy ~/.local-agent/memory/MEMORY.md from older runs is imported on first use
(migrate_legacy), so saved facts are never lost; it is left on disk, untouched.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS specs(
  project TEXT NOT NULL, job_id INTEGER NOT NULL, ts REAL NOT NULL,
  title TEXT, content TEXT NOT NULL,
  PRIMARY KEY(project, job_id));
CREATE TABLE IF NOT EXISTS handoffs(
  project TEXT NOT NULL, job_id INTEGER NOT NULL, ts REAL NOT NULL,
  parent_id INTEGER, persona TEXT, content TEXT NOT NULL,
  PRIMARY KEY(project, job_id));
CREATE TABLE IF NOT EXISTS runs(
  project TEXT NOT NULL, job_id INTEGER NOT NULL, step TEXT NOT NULL, ts REAL NOT NULL,
  content TEXT NOT NULL,
  PRIMARY KEY(project, job_id, step));
CREATE TABLE IF NOT EXISTS memory(
  project TEXT NOT NULL, ts REAL NOT NULL, fact TEXT NOT NULL,
  PRIMARY KEY(project, fact));
CREATE TABLE IF NOT EXISTS migrated(project TEXT PRIMARY KEY, ts REAL NOT NULL);
"""


def key(project: str | Path) -> str:
    """Canonical project id: its absolute, resolved path."""
    return str(Path(project).expanduser().resolve())


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=60000")
            self._local.c = c
        return c

    # ── specs ────────────────────────────────────────────────────────────
    def put_spec(self, project: str | Path, job_id: int, content: str, title: str | None = None) -> None:
        self._conn().execute("INSERT OR REPLACE INTO specs(project, job_id, ts, title, content) "
                             "VALUES(?,?,?,?,?)", (key(project), job_id, time.time(), title, content))

    def get_spec(self, project: str | Path, job_id: int) -> str | None:
        row = self._conn().execute("SELECT content FROM specs WHERE project=? AND job_id=?",
                                   (key(project), job_id)).fetchone()
        return row[0] if row else None

    def spec_count(self, project: str | Path) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM specs WHERE project=?", (key(project),)).fetchone()[0]

    def latest_spec_job(self, project: str | Path) -> int | None:
        row = self._conn().execute("SELECT job_id FROM specs WHERE project=? ORDER BY job_id DESC LIMIT 1",
                                   (key(project),)).fetchone()
        return row[0] if row else None

    # ── handoffs ─────────────────────────────────────────────────────────
    def put_handoff(self, project: str | Path, job_id: int, content: str, *,
                    parent_id: int | None = None, persona: str | None = None) -> None:
        self._conn().execute("INSERT OR REPLACE INTO handoffs(project, job_id, ts, parent_id, persona, content) "
                             "VALUES(?,?,?,?,?,?)",
                             (key(project), job_id, time.time(), parent_id, persona, content))

    def get_handoff(self, project: str | Path, job_id: int) -> str | None:
        row = self._conn().execute("SELECT content FROM handoffs WHERE project=? AND job_id=?",
                                   (key(project), job_id)).fetchone()
        return row[0] if row else None

    # ── runs (raw model replies) ─────────────────────────────────────────
    def put_run(self, project: str | Path, job_id: int, step: str, content: Any) -> None:
        body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
        self._conn().execute("INSERT OR REPLACE INTO runs(project, job_id, step, ts, content) "
                             "VALUES(?,?,?,?,?)", (key(project), job_id, str(step), time.time(), body))

    def get_runs(self, project: str | Path, job_id: int) -> list[dict]:
        rows = self._conn().execute("SELECT step, ts, content FROM runs WHERE project=? AND job_id=? "
                                    "ORDER BY ts, step", (key(project), job_id)).fetchall()
        return [{"step": r["step"], "ts": r["ts"], "content": r["content"]} for r in rows]

    def run_count(self, project: str | Path, job_id: int) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM runs WHERE project=? AND job_id=?",
                                    (key(project), job_id)).fetchone()[0]

    # ── memory ───────────────────────────────────────────────────────────
    def add_memory(self, project: str | Path, fact: str) -> bool:
        """Save one durable fact. Returns False if it was already remembered."""
        fact = " ".join(str(fact).split())
        if not fact:
            return False
        self.migrate_legacy(project)
        cur = self._conn().execute("INSERT OR IGNORE INTO memory(project, ts, fact) VALUES(?,?,?)",
                                   (key(project), time.time(), fact))
        return cur.rowcount > 0

    def memory_facts(self, project: str | Path, limit: int = 500) -> list[tuple[float, str]]:
        self.migrate_legacy(project)
        rows = self._conn().execute("SELECT ts, fact FROM memory WHERE project=? ORDER BY ts LIMIT ?",
                                    (key(project), limit)).fetchall()
        return [(r["ts"], r["fact"]) for r in rows]

    def memory_markdown(self, project: str | Path, limit_chars: int = 6000) -> str:
        facts = self.memory_facts(project)
        if not facts:
            return ""
        lines = [f"- {time.strftime('%Y-%m-%d', time.localtime(ts))}: {fact}" for ts, fact in facts]
        return "\n".join(lines)[:limit_chars].strip()

    # ── retention / pruning ──────────────────────────────────────────────
    def prune(self, keep_jobs: int = 200, keep_days: float = 30) -> dict:
        """Trim debug history so the store can't grow forever: per project keep the newest
        `keep_jobs` jobs, and drop anything older than `keep_days` (0 for either = no limit).
        Specs, handoffs and raw runs are pruned; durable `memory` facts never are. Returns how
        many rows each table lost."""
        c = self._conn()
        removed = {"specs": 0, "handoffs": 0, "runs": 0}
        if keep_days and keep_days > 0:
            cutoff = time.time() - keep_days * 86400
            for tbl in list(removed):
                removed[tbl] += c.execute(f"DELETE FROM {tbl} WHERE ts < ?", (cutoff,)).rowcount
        if keep_jobs and keep_jobs > 0:
            projects = {r[0] for r in c.execute("SELECT project FROM runs "
                                                "UNION SELECT project FROM specs "
                                                "UNION SELECT project FROM handoffs").fetchall()}
            for p in projects:
                # distinct job ids for this project; job_id is globally monotonic, so a numeric
                # sort orders them by time. Keep the newest `keep_jobs`, drop everything older.
                jids = sorted({int(r[0]) for r in c.execute(
                    "SELECT job_id FROM runs WHERE project=? "
                    "UNION SELECT job_id FROM specs WHERE project=? "
                    "UNION SELECT job_id FROM handoffs WHERE project=?", (p, p, p)).fetchall()},
                    reverse=True)
                if len(jids) <= keep_jobs:
                    continue
                thr = jids[keep_jobs]                      # this job id and all older ones go
                for tbl in list(removed):
                    removed[tbl] += c.execute(f"DELETE FROM {tbl} WHERE project=? AND job_id<=?",
                                              (p, thr)).rowcount
        return removed

    # ── legacy import ────────────────────────────────────────────────────
    def migrate_legacy(self, project: str | Path) -> int:
        """Import facts from a pre-existing <project>/.local-agent/memory/MEMORY.md once."""
        pk = key(project)
        if self._conn().execute("SELECT 1 FROM migrated WHERE project=?", (pk,)).fetchone():
            return 0
        self._conn().execute("INSERT OR REPLACE INTO migrated(project, ts) VALUES(?,?)", (pk, time.time()))
        md = Path(pk) / ".local-agent" / "memory" / "MEMORY.md"
        try:
            text = md.read_text(errors="replace")
        except OSError:
            return 0
        n = 0
        for line in text.splitlines():
            m = re.match(r"\s*-\s*(?:\d{4}-\d{2}-\d{2}:\s*)?(.+)", line)
            if not m:
                continue
            fact = " ".join(m.group(1).split())
            if fact and fact not in ("No memories yet.",):
                cur = self._conn().execute("INSERT OR IGNORE INTO memory(project, ts, fact) VALUES(?,?,?)",
                                           (pk, time.time(), fact))
                n += cur.rowcount
        return n
