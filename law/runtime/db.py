"""Durable job queue in SQLite (WAL). Shared by the worker, the gateway, the CLI and the
Telegram bot.

jobs    one row per job. kind: chat | probe | agent. status:
        queued → running → done | failed | cancelled      (running → queued on retry/restart)
        running → waiting → queued   (an agent that handed work to sub-jobs waits for them;
                                      it is put back in the queue when the last one ends)
events  append-only per-job stream: status, model, deltas, probe, agent, spec, snapshot,
        warning, done, error, cancelled
worker  single heartbeat row (pid, beat = process alive, progress = job moving, job, model, state)
watchdog_log  what the watchdog restarted and why

A job interrupted too often (worker crash / restart while it ran) is failed instead of
retried forever — see max_interruptions. Agent jobs keep their conversation in jobs.state,
so a retried, restarted or resumed agent continues where it stopped.

Every writer uses BEGIN IMMEDIATE, so claiming a job is atomic: two workers can never
take the same job, and a crashed worker's jobs are put back with requeue_running().
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

TERMINAL = ("done", "failed", "cancelled")
ACTIVE = ("queued", "running", "waiting")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id INTEGER,
  kind      TEXT NOT NULL DEFAULT 'chat',
  model     TEXT NOT NULL,
  persona   TEXT,
  source    TEXT,
  payload   TEXT NOT NULL,
  status    TEXT NOT NULL DEFAULT 'queued',
  priority  INTEGER NOT NULL DEFAULT 0,
  attempts  INTEGER NOT NULL DEFAULT 0,
  created   REAL NOT NULL,
  started   REAL,
  finished  REAL,
  error     TEXT,
  result    TEXT,
  interruptions INTEGER NOT NULL DEFAULT 0,
  state     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(status, priority DESC, id);
CREATE TABLE IF NOT EXISTS events(
  job_id INTEGER NOT NULL,
  seq    INTEGER NOT NULL,
  ts     REAL NOT NULL,
  type   TEXT NOT NULL,
  data   TEXT,
  PRIMARY KEY(job_id, seq)
);
CREATE TABLE IF NOT EXISTS worker(
  id     INTEGER PRIMARY KEY CHECK(id = 1),
  pid    INTEGER,
  beat   REAL,
  job_id INTEGER,
  model  TEXT,
  state  TEXT,
  progress REAL
);
CREATE TABLE IF NOT EXISTS watchdog_log(
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL NOT NULL,
  target TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT
);
"""

# columns added after Phase 1 — added in place to existing databases
MIGRATIONS = [("jobs", "interruptions", "INTEGER NOT NULL DEFAULT 0"),
              ("worker", "progress", "REAL"),
              ("jobs", "state", "TEXT")]
DEFAULT_MAX_INTERRUPTIONS = 3


class Queue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        c = self._conn()
        c.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:     # another process added it first
                    pass

    # ── connection / transactions ───────────────────────────────────────
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

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    @staticmethod
    def _event(c: sqlite3.Connection, job_id: int, type_: str, data: Any) -> None:
        seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE job_id=?", (job_id,)).fetchone()[0]
        c.execute("INSERT INTO events(job_id, seq, ts, type, data) VALUES(?,?,?,?,?)",
                  (job_id, seq, time.time(), type_, json.dumps(data)))

    def _wake_parent(self, c: sqlite3.Connection, job_id: int) -> None:
        """A sub-job ended: if its parent waits and has no other active sub-jobs, queue it."""
        row = c.execute("SELECT parent_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        pid = row[0] if row else None
        if not pid:
            return
        p = c.execute("SELECT status FROM jobs WHERE id=?", (pid,)).fetchone()
        if not p or p[0] != "waiting":
            return
        busy = c.execute("SELECT COUNT(*) FROM jobs WHERE parent_id=? AND status IN ('queued','running','waiting')",
                         (pid,)).fetchone()[0]
        if busy == 0:
            c.execute("UPDATE jobs SET status='queued' WHERE id=?", (pid,))
            self._event(c, pid, "status", {"state": "queued", "reason": "sub-jobs finished"})

    # ── jobs ────────────────────────────────────────────────────────────
    def enqueue(self, model: str, payload: dict, *, persona: str | None = None, source: str = "cli",
                parent_id: int | None = None, priority: int = 0, kind: str = "chat") -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO jobs(parent_id, kind, model, persona, source, payload, priority, created)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (parent_id, kind, model, persona, source, json.dumps(payload), priority, time.time()))
            jid = cur.lastrowid
            self._event(c, jid, "status", {"state": "queued"})
            return jid

    def claim(self) -> dict | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE status='queued' "
                            "ORDER BY priority DESC, id LIMIT 1").fetchone()
            if row is None:
                return None
            c.execute("UPDATE jobs SET status='running', started=?, attempts=attempts+1 WHERE id=?",
                      (time.time(), row["id"]))
            job = dict(row)
            job.update(status="running", attempts=row["attempts"] + 1)
            return job

    def finish(self, job_id: int, status: str, *, result: Any = None, error: str | None = None) -> bool:
        """Only a RUNNING job can finish — a job cancelled meanwhile stays cancelled."""
        assert status in ("done", "failed")
        with self._tx() as c:
            n = c.execute("UPDATE jobs SET status=?, finished=?, result=?, error=? "
                          "WHERE id=? AND status='running'",
                          (status, time.time(), json.dumps(result) if result is not None else None,
                           error, job_id)).rowcount
            if n:
                if status == "done":
                    self._event(c, job_id, "done", result)
                else:
                    self._event(c, job_id, "error", {"message": error})
                self._wake_parent(c, job_id)
            return bool(n)

    def _interrupt(self, c, jid: int, reason: str, max_interruptions: int) -> str:
        """Put an interrupted job back — or fail it if it keeps getting interrupted."""
        n = c.execute("SELECT interruptions FROM jobs WHERE id=?", (jid,)).fetchone()[0] + 1
        if n >= max_interruptions:
            msg = (f"interrupted {n} times ({reason}) — giving up so it can't loop forever. "
                   "This usually means the Mac ran out of memory on this job; check `law logs worker`.")
            c.execute("UPDATE jobs SET status='failed', finished=?, error=?, interruptions=? WHERE id=?",
                      (time.time(), msg, n, jid))
            self._event(c, jid, "error", {"message": msg})
            self._wake_parent(c, jid)
            return "failed"
        c.execute("UPDATE jobs SET status='queued', interruptions=? WHERE id=?", (n, jid))
        self._event(c, jid, "status", {"state": "queued", "reason": reason})
        return "queued"

    def requeue(self, job_id: int, *, reason: str, undo_attempt: bool = False,
                max_interruptions: int | None = None) -> bool:
        """Retry after an error (attempt counted), or put back after an interruption
        (undo_attempt=True: the attempt doesn't count, but the interruption does)."""
        with self._tx() as c:
            st = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not st or st[0] != "running":
                return False
            if undo_attempt:
                c.execute("UPDATE jobs SET attempts=attempts-1 WHERE id=?", (job_id,))
                return self._interrupt(c, job_id, reason,
                                       max_interruptions or DEFAULT_MAX_INTERRUPTIONS) == "queued"
            c.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
            self._event(c, job_id, "status", {"state": "queued", "reason": reason})
            return True

    def requeue_running(self, max_interruptions: int = DEFAULT_MAX_INTERRUPTIONS) -> list[int]:
        """Called on worker start: anything left 'running' was interrupted by a crash.
        Returns the ids put back in the queue (jobs failed for looping are not included)."""
        with self._tx() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM jobs WHERE status='running'")]
            return [jid for jid in ids
                    if self._interrupt(c, jid, "worker restarted", max_interruptions) == "queued"]

    def _cancel(self, c, job_id: int, reason: str) -> bool:
        n = c.execute("UPDATE jobs SET status='cancelled', finished=?, error=? "
                      "WHERE id=? AND status IN ('queued','running','waiting')",
                      (time.time(), reason, job_id)).rowcount
        if n:
            self._event(c, job_id, "cancelled", {"reason": reason})
            for (child,) in c.execute("SELECT id FROM jobs WHERE parent_id=? AND status IN "
                                      "('queued','running','waiting')", (job_id,)).fetchall():
                self._cancel(c, child, f"parent job {job_id} cancelled")
        return bool(n)

    def cancel(self, job_id: int, reason: str = "cancelled") -> bool:
        """Cancel a job and all its active sub-jobs. A cancelled sub-job wakes its parent."""
        with self._tx() as c:
            ok = self._cancel(c, job_id, reason)
            if ok:
                self._wake_parent(c, job_id)
            return ok

    def wait_for_children(self, job_id: int, children: list[int]) -> bool:
        """running → waiting until every sub-job has ended (then _wake_parent requeues it)."""
        with self._tx() as c:
            n = c.execute("UPDATE jobs SET status='waiting' WHERE id=? AND status='running'",
                          (job_id,)).rowcount
            if n:
                self._event(c, job_id, "status", {"state": "waiting", "children": children})
                busy = c.execute("SELECT COUNT(*) FROM jobs WHERE parent_id=? AND status IN "
                                 "('queued','running','waiting')", (job_id,)).fetchone()[0]
                if busy == 0:                      # children already ended (or none)
                    c.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
                    self._event(c, job_id, "status", {"state": "queued", "reason": "sub-jobs finished"})
            return bool(n)

    def revive(self, job_id: int) -> bool:
        """Resume a failed / cancelled job: back in the queue with a fresh retry budget.
        Agent jobs continue from their saved conversation."""
        with self._tx() as c:
            n = c.execute("UPDATE jobs SET status='queued', attempts=0, interruptions=0, error=NULL, "
                          "finished=NULL WHERE id=? AND status IN ('failed','cancelled')", (job_id,)).rowcount
            if n:
                self._event(c, job_id, "status", {"state": "queued", "reason": "resumed"})
            return bool(n)

    def add_event(self, job_id: int, type_: str, data: Any = None) -> None:
        with self._tx() as c:
            self._event(c, job_id, type_, data)

    # ── agent state ─────────────────────────────────────────────────────
    def save_state(self, job_id: int, state: Any) -> None:
        with self._tx() as c:
            c.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps(state), job_id))

    def get_state(self, job_id: int) -> Any:
        row = self._conn().execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def children(self, job_id: int) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM jobs WHERE parent_id=? ORDER BY id", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── reads ───────────────────────────────────────────────────────────
    def get(self, job_id: int) -> dict | None:
        row = self._conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def status(self, job_id: int) -> str | None:
        row = self._conn().execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def events(self, job_id: int, after: int = 0) -> list[dict]:
        rows = self._conn().execute("SELECT seq, ts, type, data FROM events WHERE job_id=? AND seq>? "
                                    "ORDER BY seq", (job_id, after)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"],
                 "data": json.loads(r["data"]) if r["data"] else None} for r in rows]

    def jobs(self, limit: int = 20) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, since_ts: float = 0.0, limit: int = 300) -> list[dict]:
        """Events across ALL jobs newer than `since_ts`, oldest first (for the live dashboard)."""
        rows = self._conn().execute(
            "SELECT job_id, seq, ts, type, data FROM events WHERE ts>? ORDER BY ts, job_id, seq LIMIT ?",
            (since_ts, limit)).fetchall()
        return [{"job_id": r["job_id"], "seq": r["seq"], "ts": r["ts"], "type": r["type"],
                 "data": json.loads(r["data"]) if r["data"] else None} for r in rows]

    def depth(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]

    def position(self, job_id: int) -> int:
        """1 = next in line. 0 = not queued."""
        row = self._conn().execute("SELECT priority, status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "queued":
            return 0
        ahead = self._conn().execute(
            "SELECT COUNT(*) FROM jobs WHERE status='queued' AND (priority>? OR (priority=? AND id<?))",
            (row["priority"], row["priority"], job_id)).fetchone()[0]
        return ahead + 1

    # ── worker heartbeat ────────────────────────────────────────────────
    def beat(self, pid: int, job_id: int | None, model: str | None, state: str,
             progress: float | None = None) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO worker(id, pid, beat, job_id, model, state, progress) "
                      "VALUES(1,?,?,?,?,?,?)", (pid, time.time(), job_id, model, state, progress or time.time()))

    def worker_info(self) -> dict | None:
        row = self._conn().execute("SELECT * FROM worker WHERE id=1").fetchone()
        if not row:
            return None
        d = dict(row)
        now = time.time()
        d["age_s"] = round(now - (d["beat"] or 0), 1)
        d["progress_age_s"] = round(now - (d.get("progress") or d["beat"] or 0), 1)
        return d

    def running_job(self) -> dict | None:
        row = self._conn().execute("SELECT * FROM jobs WHERE status='running' ORDER BY started LIMIT 1").fetchone()
        return dict(row) if row else None

    # ── watchdog log ────────────────────────────────────────────────────
    def log_watchdog(self, target: str, action: str, reason: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO watchdog_log(ts, target, action, reason) VALUES(?,?,?,?)",
                      (time.time(), target, action, reason))
            c.execute("DELETE FROM watchdog_log WHERE id NOT IN "
                      "(SELECT id FROM watchdog_log ORDER BY id DESC LIMIT 500)")

    def watchdog_log(self, limit: int = 10) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM watchdog_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
