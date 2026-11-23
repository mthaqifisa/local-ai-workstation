#!/usr/bin/env python3
"""Per-ticket agent metrics from LAW's job events: minutes to the first save, nudges, minutes to the commit.

    ticket_metrics.py <job-id> [<job-id> ...]

Used to measure a model or agent change on real tickets (ENHANCE brief §4.5): compare against the logged baseline
(2026-10-27 trial: 53-78 min to the first save, 106-149 min per ticket). Reads the replayable event lists the job
store keeps in Valkey; read-only.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from law import registry  # noqa: E402 - the repo's own package, importable once src/ is on the path
from law.api import redis_url  # noqa: E402
from law.paths import LawPaths  # noqa: E402
from law.queue.store import JobStore  # noqa: E402

COMMIT = re.compile(r"git commit -m [\"']?([A-Z]+-[\d.]+)")


@dataclass
class Ticket:
    start: float
    ticket: str | None = None
    first_save: float | None = None
    commit: float | None = None
    nudges: int = 0

    def minutes(self, end: float | None) -> int | None:
        return round((end - self.start) / 60) if end is not None else None


def tickets(events: list[dict[str, Any]]) -> list[Ticket]:
    """Split a job's events into ticket sessions: a session starts when the coder agent starts (or the job does)
    and ends at its first commit of a ticket ID; the next agent start after a commit opens the next session."""
    rows = sorted((float(e["t"]), str(e.get("event", "")), str((e.get("data") or {}).get("message", "")))
                  for e in events)
    out: list[Ticket] = []
    cur: Ticket | None = None
    for t, event, msg in rows:
        opens = event == "started" or msg.startswith("coder agent working")
        if cur is None or (cur.commit is not None and opens):
            cur = Ticket(start=t)
            out.append(cur)
        if msg.startswith("✏️") and cur.first_save is None:
            cur.first_save = t
        if msg.startswith("⚠️"):
            cur.nudges += 1
        found = COMMIT.search(msg)
        if found and cur.commit is None:
            cur.ticket, cur.commit = found.group(1), t
    return out


def render(rows: list[Ticket]) -> str:
    def clock(s: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(s))

    lines = ["| Ticket | Start | First save (min) | Nudges | Commit (min) |", "|---|---|---|---|---|"]
    for k in rows:
        first, done = k.minutes(k.first_save), k.minutes(k.commit)
        lines.append(f"| {k.ticket or '?'} | {clock(k.start)} | {'-' if first is None else first} | {k.nudges} | "
                     f"{'-' if done is None else done} |")
    return "\n".join(lines)


async def load(job_ids: list[str]) -> list[dict[str, Any]]:
    paths = LawPaths.from_env()
    store = JobStore.connect(redis_url(registry.load(paths.registry_file)))
    events: list[dict[str, Any]] = []
    for job in job_ids:
        events += await store.events(job)
    return events


def main(argv: list[str] | None = None) -> int:
    jobs = (argv if argv is not None else sys.argv[1:])
    if not jobs:
        print(__doc__)
        return 2
    print(render(tickets(asyncio.run(load(jobs)))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
