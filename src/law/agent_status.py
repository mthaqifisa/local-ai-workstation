"""What each job's agents are doing right now: one status per top-level job, shared by the Console's Agents tab and
Tony's `/jobs`. It answers "is it working, thinking, stuck, or dead?" from the queue, the job's progress events, the
project's `.law/` files and the model server's activity.

Health:
- working  : an agent action (read, command, edit, fetch) in the last 5 minutes
- thinking : no action for a while, but the model is generating (long reasoning or a large file)
- stalled  : no file change for 20 minutes while running (the stall guard nudges at 25)
- queued   : waiting for its turn on the model
- stuck    : a multi-step job with no step running and none failed (needs Restart)
- failed / cancelled / done
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx

from law.queue.jobs import Job, Status
from law.queue.store import JobStore

ACTIVE_S = 300
STALL_S = 1200
COMPLETION = re.compile(r"Chat completion: model=(\S+), (\d+) tokens in ([\d.]+)s \(([\d.]+) tok/s\), prompt: (\d+)")
TICK = re.compile(r"^\s*- \[[xX]\]", re.MULTILINE)
OPEN = re.compile(r"^\s*- \[ \]\s*(.+)$", re.MULTILINE)


def model_activity(llm_internal: str, log: Path) -> dict[str, Any]:
    """Is the model generating now, and how big and fast was its last step (from oMLX's status and log)."""
    out: dict[str, Any] = {"active": None, "last": None}
    try:
        status = httpx.get(f"{llm_internal}/api/status", timeout=3).json()
        out["active"] = int(status.get("active_requests", 0)) > 0
        out["loaded"] = status.get("loaded_models", [])
    except (httpx.HTTPError, ValueError):
        pass
    try:
        with log.open("rb") as fh:
            fh.seek(max(0, log.stat().st_size - 200_000))
            tail = fh.read().decode(errors="replace")
    except OSError:
        return out
    matches = COMPLETION.findall(tail)
    if matches:
        model, tokens, secs, tps, prompt = matches[-1]
        out["last"] = {"model": model, "tokens": int(tokens), "seconds": float(secs), "tok_s": float(tps),
                       "context": int(prompt)}
    pressure = re.findall(r"Memory pressure level: \w+ -> (\w+) \(current=([\d.]+)GB", tail)
    if pressure:
        out["memory"] = {"level": pressure[-1][0], "gb": float(pressure[-1][1])}
    return out


def plan_summary(project: Path) -> dict[str, Any] | None:
    """Ticked/total items in .law/PLAN.md, the next open item, and the latest PROGRESS.md entry."""
    law = project / ".law"
    plan = law / "PLAN.md"
    if not plan.is_file():
        return None
    text = plan.read_text(errors="replace")
    done, todo = len(TICK.findall(text)), OPEN.findall(text)
    progress = (law / "PROGRESS.md").read_text(errors="replace") if (law / "PROGRESS.md").is_file() else ""
    entries = [e.strip() for e in re.split(r"\n(?=#{2,3} |- \*\*|\d{4}-\d\d-\d\d)", progress) if e.strip()]
    latest = entries[-1] if len(entries) > 1 else ""
    return {"done": done, "total": done + len(todo), "next": todo[0].strip() if todo else None,
            "progress": latest[-600:]}


def _dir(job: Job, children: list[Job]) -> Path | None:
    for j in (job, *reversed(children)):
        for source in (j.payload, j.payload.get("_carry") or {}, j.result or {}):
            if source.get("dir"):
                return Path(str(source["dir"])).expanduser()
    return None


def _title(job: Job) -> str:
    p = job.payload
    fields = p.get("fields") or {}
    text = fields.get("name") or p.get("project") or p.get("prompt") or p.get("text") or p.get("query") or job.kind
    return str(text).strip().splitlines()[0][:100]


def health(job: Job, stage: Job | None, last_action: float | None, last_write: float | None,
           model_active: bool | None, now: float) -> str:
    if job.status is Status.DONE:
        return "done"
    if job.status in {Status.FAILED, Status.DEAD}:
        return "failed"
    if job.status is Status.CANCELLED:
        return "cancelled"
    target = stage or job
    if target.status is Status.QUEUED:
        return "queued"
    if stage is None and job.payload.get("_stages"):
        return "stuck"
    if target.status is not Status.RUNNING:
        return "stuck"
    started = target.started or now
    if last_action and now - last_action < ACTIVE_S:
        return "working"
    if now - max(last_write or 0, started) >= STALL_S:
        return "stalled"
    return "thinking" if model_active is not False else "stalled"


async def job_status(store: JobStore, job: Job, model: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    children = await store.children(job.id) if job.payload.get("_stages") else []
    live = [c for c in children if c.status in {Status.QUEUED, Status.RUNNING}]
    stage = live[-1] if live else (children[-1] if children else None)
    watched = stage if stage is not None and live else (None if job.payload.get("_stages") else job)
    events = (await store.events(watched.id))[-200:] if watched else []
    actions = [e for e in events if e.get("event") == "progress"]
    writes = [e for e in actions if str(e.get("data", {}).get("message", "")).startswith("✏️")]
    last_action = actions[-1]["t"] if actions else None
    last_write = writes[-1]["t"] if writes else None
    total = len(job.payload.get("_stages") or [])
    state = health(job, stage if live else None, last_action, last_write, model.get("active"), now)
    project = _dir(job, children)
    recent = [{"t": e["t"], "message": str(e.get("data", {}).get("message", ""))[:160]} for e in actions[-8:]]
    window = [e for e in actions if now - e["t"] < 600]

    def count(prefix: str) -> int:
        return sum(1 for e in window if str(e.get("data", {}).get("message", "")).startswith(prefix))

    return {
        "id": job.id, "kind": job.kind, "title": _title(job), "source": job.source, "status": job.status.value,
        "health": state, "created": job.created, "error": job.error,
        "stage": None if stage is None else {
            "index": stage.stage + 1, "total": total, "kind": stage.kind, "role": stage.payload.get("role"),
            "status": stage.status.value, "started": stage.started, "id": stage.id},
        "last_action": recent[-1] if recent else None, "last_write": last_write, "recent": recent,
        "last_10min": {"reads": count("📖"), "commands": count("▶"), "edits": count("✏️"), "web": count("🌐")},
        "project": str(project) if project else None,
        "plan": plan_summary(project) if project and project.is_dir() else None,
        "actions": actions_for(state, bool(total)),
    }


def actions_for(state: str, pipeline: bool) -> list[str]:
    if state in {"failed", "cancelled"}:
        return ["retry", "skip"] if pipeline else ["retry"]
    if state in {"stalled", "stuck"}:
        return ["restart", "cancel"] if pipeline else ["cancel"]
    if state in {"working", "thinking", "queued"}:
        return ["cancel"]
    return []


def matches(job: Job, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    haystack = " ".join([job.id, job.kind, _title(job), str(job.payload.get("project", "")),
                         str((job.payload.get("fields") or {}).get("repo", "")), str(job.payload.get("dir", ""))])
    return q in haystack.lower()


async def statuses(store: JobStore, model: dict[str, Any], query: str = "", recent_s: float = 86400,
                   limit: int = 12) -> list[dict[str, Any]]:
    """Live top-level jobs first, then ones that stopped in the last day (failed ones offer Retry)."""
    now = time.time()
    jobs = [j for j in await store.history(300) if j.parent is None and matches(j, query)]
    live = [j for j in jobs if j.status in {Status.QUEUED, Status.RUNNING, Status.WAITING}]
    stopped = [j for j in jobs if j.status in {Status.FAILED, Status.DEAD, Status.CANCELLED}
               and now - (j.finished or j.created) < recent_s]
    finished = [j for j in jobs if j.status is Status.DONE and now - (j.finished or j.created) < 3600] if query else []
    picked = (live + stopped + finished)[:limit]
    return [await job_status(store, j, model, now) for j in picked]
