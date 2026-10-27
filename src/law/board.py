"""Project board for the Console's Projects tab: one Jira-style board per project, built from what LAW already
records, so it never drifts from the work.

Tickets come from:
- the pipeline stages of the project's jobs: proposal writing, its review (QA), render and email; the environment
  stage; each QA review round and the fix round after it (the bugs QA reported); the handover document;
- the owner's gates (go / no go on a proposal or plan, verification of a build): "waiting for approval" until the
  owner decides in Telegram;
- the development tickets the coder writes in `.law/PLAN.md` ("- [ ] P-6 ..." under "### M2 ..." milestones),
  with their commits ("P-6: ...") and `.law/PROGRESS.md` entries as logs.

Notes (the owner's, from the Console or Telegram `/note`, and progress notes from the build sessions) live in
`~/LAW/state/boards/<project>.json`. A note names its ticket, or is pinned to the ticket in progress when it was
written.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from law import bugs
from law.paths import LawPaths
from law.projects import Project, Projects
from law.queue.jobs import Job, Status, new_id
from law.queue.store import JobStore

Column = Literal["todo", "progress", "approval", "done", "failed"]
COLUMNS: list[tuple[Column, str]] = [("todo", "To do"), ("progress", "In progress"),
                                     ("approval", "Waiting for approval"), ("done", "Completed"),
                                     ("failed", "Failed / cancelled")]
LIVE = {Status.QUEUED, Status.RUNNING, Status.WAITING}
STOPPED = {Status.FAILED, Status.DEAD, Status.CANCELLED}
DECISIONS = {"go": "Go", "nogo": "No go", "changes": "Needs changes", "allgood": "All good", "resume": "Resumed"}
LOG_LIMIT = 300

TICKET = re.compile(r"^(\s*)- \[([ xX])\]\s*([A-Z][A-Z0-9]*-\d+(?:\.\d+)?)\b[:.]?\s*(.*)$")
MILESTONE = re.compile(r"^ {0,3}#{3,}\s+(.+?)\s*$")
STAMP = re.compile(r"(\d{4}-\d\d-\d\d)(?:[ T](\d\d:\d\d))?")


class Note(BaseModel):
    id: str = Field(default_factory=new_id)
    t: float = Field(default_factory=time.time)
    ticket: str | None = None  # None: the ticket in progress at `t`
    author: str = "owner"
    text: str


class NoteIn(BaseModel):
    text: str
    ticket: str | None = None
    author: str = "owner"
    at: float | None = None  # back-dating a note (e.g. a progress email sent earlier)
    to_agent: bool = False  # also deliver it to the agent working on the project (like Telegram /note)


# ---- notes ---------------------------------------------------------------------------------------------------
def _notes_file(paths: LawPaths, pid: str) -> Path:
    return paths.state / "boards" / f"{pid}.json"


def notes(paths: LawPaths, pid: str) -> list[Note]:
    try:
        data = json.loads(_notes_file(paths, pid).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [Note.model_validate(n) for n in data.get("notes", [])] if isinstance(data, dict) else []


def _save_notes(paths: LawPaths, pid: str, items: list[Note]) -> None:
    path = _notes_file(paths, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"notes": [n.model_dump() for n in items]}, indent=2) + "\n")
    tmp.replace(path)


def add_note(paths: LawPaths, pid: str, text: str, *, ticket: str | None = None, author: str = "owner",
             at: float | None = None) -> Note:
    if not text.strip():
        raise ValueError("the note is empty")
    note = Note(text=text.strip(), ticket=(ticket or "").strip() or None, author=author.strip() or "owner",
                t=at or time.time())
    _save_notes(paths, pid, [*notes(paths, pid), note])
    return note


def delete_note(paths: LawPaths, pid: str, note_id: str) -> None:
    items = notes(paths, pid)
    kept = [n for n in items if n.id != note_id]
    if len(kept) == len(items):
        raise KeyError(note_id)
    _save_notes(paths, pid, kept)


# ---- tickets ---------------------------------------------------------------------------------------------------
class Ticket:
    def __init__(self, tid: str, title: str, kind: str, epic: str, order: float) -> None:
        self.id, self.title, self.kind, self.epic, self.order = tid, title, kind, epic, order
        self.column: Column = "todo"
        self.state = ""  # finer status for the card: queued, running, failed, cancelled, a decision
        self.details = ""
        self.started: float | None = None
        self.finished: float | None = None
        self.jobs: list[str] = []
        self.root = ""  # the pipeline whose attempt counts (a later run supersedes an earlier, stopped one)
        self.steps: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []

    def log(self, t: float | None, source: str, text: str) -> None:
        if text.strip():
            self.logs.append({"t": t, "source": source, "text": text.strip()})

    def as_dict(self) -> dict[str, Any]:
        logs = sorted(self.logs, key=lambda e: e["t"] or 0)[-LOG_LIMIT:]
        return {"id": self.id, "title": self.title, "kind": self.kind, "epic": self.epic, "column": self.column,
                "state": self.state, "details": self.details, "started": self.started, "finished": self.finished,
                "jobs": self.jobs, "steps": self.steps, "logs": logs,
                "notes": sorted(self.notes, key=lambda n: n["t"])}


def _belongs(job: Job, pid: str) -> bool:
    p = job.payload
    return p.get("project") == pid or (p.get("fields") or {}).get("repo") == pid


def _stage_group(spec: dict[str, Any], phase: str, version: int, qa_round: list[int]) -> tuple[str, str, str]:
    """(ticket id, title, kind) for one pipeline stage; stages of one step share a ticket."""
    kind, p = spec["kind"], spec.get("payload", {})
    ref = str(p.get("spec_ref", ""))
    v = f"v{version}"
    if kind == "project_gate":
        gate = p.get("gate", "")
        title = {"proposal": f"Go / no go: proposal {v}", "plan": f"Go / no go: plan {v}"}.get(
            gate, f"Owner verification of build {v}")
        return f"approve-{gate}-{v}", title, "approval"
    if kind == "code_plan":
        return f"plan-{v}", f"Write the plan {v}", "proposal"
    if phase == "bugfix":  # a /bugfix pipeline (fix, then QA retests) or a QA run (/qa)
        if p.get("qa_run"):
            run = str(p["qa_run"])
            return (f"qarun-{run[7:].replace(' ', '-')}", run, "qa") if kind == "task_review" else ("", "", "")
        which = str(p.get("bugs") or "reported bugs")
        if kind == "task_fix":
            return f"fix-{which}", f"Fix {which}", "bugfix"
        if kind == "task_review":
            return f"retest-{which}", f"QA retest of {which}", "qa"
        return "", "", ""
    if phase == "proposal":
        if kind in {"task_review", "task_fix"}:
            return f"proposal-{v}-qa", f"Review proposal {v} (QA) and apply fixes", "qa"
        if kind in {"proposal_render", "proposal_mail"}:
            return f"proposal-{v}-deliver", f"Render and email proposal {v}", "docs"
        return f"proposal-{v}", f"Research and write proposal {v}", "proposal"
    if ref == "project.env":
        return f"env-{v}", "Prepare the Mac environment (tools and versions)", "setup"
    if ref == "project.handover" or kind in {"proposal_render", "proposal_mail"}:
        return f"handover-{v}", f"Handover document {v} (screenshots, render, email)", "docs"
    if kind == "task_review":
        qa_round[0] += 1
        return f"qa-{v}-{qa_round[0]}", f"QA review round {qa_round[0]}", "qa"
    if kind == "task_fix":
        return f"fix-{v}-{qa_round[0]}", f"Fix the bugs QA reported (round {qa_round[0]})", "bugfix"
    return "", "", ""  # a milestone session: its work shows as the PLAN.md tickets


def _phase(root: Job) -> tuple[str, int]:
    specs = root.payload.get("_stages") or []
    refs = {str(s.get("payload", {}).get("spec_ref", "")) for s in specs}
    version = next((int(s["payload"]["version"]) for s in specs if "version" in s.get("payload", {})), 1)
    if "project.dev" in refs:
        return "develop", version
    if any(s["kind"] == "project_bug_report" for s in specs):
        return "bugfix", version
    if any(s["kind"] == "code_plan" for s in specs) or "proposal.write" in refs:
        return "proposal", version
    return "", version


def _pick(attempts: list[Job]) -> Job:
    """The attempt that counts: the live one, else the latest."""
    live = [j for j in attempts if j.status in LIVE]
    return live[-1] if live else max(attempts, key=lambda j: j.finished or j.started or j.created)


def _column(job: Job | None) -> tuple[Column, str]:
    if job is None:
        return "todo", ""
    if job.status in LIVE:
        return "progress", job.status.value
    if job.status is Status.DONE:
        return "done", ""
    return "failed", "cancelled" if job.status is Status.CANCELLED else "failed"


def _event_text(e: dict[str, Any]) -> str:
    data = e.get("data") or {}
    if e.get("event") == "progress":
        return str(data.get("message", ""))
    if e.get("event") in {"failed", "dead", "cancelled"}:
        return f"{e['event']}: {data.get('error', '')}".strip(": ")
    if e.get("event") in {"started", "requeued", "done"}:
        extra = data.get("reason") or (f"attempt {data['attempt']}" if data.get("attempt") else "")
        return f"{e['event']}{' (' + str(extra) + ')' if extra else ''}"
    return ""


async def _stage_tickets(store: JobStore, project: Project, roots: list[Job], tickets: dict[str, Ticket],
                         dev_jobs: dict[int, list[Job]], dev_order: dict[int, float]) -> None:
    phased = [(root, *_phase(root)) for root in roots]
    phased = [x for x in phased if x[1]]  # e.g. a "resend the email" job is not a phase
    for root_n, (root, phase, version) in enumerate(phased):
        children = await store.children(root.id)
        by_stage: dict[int, list[Job]] = {}
        for c in children:
            by_stage.setdefault(c.stage, []).append(c)
        qa_round = [0]
        # a stage that stopped while a later one ran was skipped by the owner
        reached = max((c.stage for c in children if c.status is Status.DONE or c.status in LIVE), default=-1)
        for i, spec in enumerate(root.payload.get("_stages") or []):
            attempts = by_stage.get(i, [])
            ref = spec.get("payload", {}).get("spec_ref")
            if ref == "project.dev":
                dev_jobs.setdefault(version, []).extend(attempts)
                dev_order.setdefault(version, root_n * 1000 + i)  # the tickets sit where the milestones run
                continue
            tid, title, kind = _stage_group(spec, phase, version, qa_round)
            if not tid:
                continue
            if not attempts and root.status in STOPPED | {Status.DONE}:
                continue  # a stopped pipeline will not reach this stage (unless retried)
            t = tickets.get(tid)
            if t is None:
                epic = f"Proposal v{version}" if phase == "proposal" else f"Build v{version}"
                t = tickets[tid] = Ticket(tid, title, kind, epic, root_n * 1000 + i)
            if t.root != root.id:
                t.root, t.steps, t.finished = root.id, [], None
            job = _pick(attempts) if attempts else None
            column, state = _column(job)
            if column == "failed" and i < reached:
                column, state = "done", "skipped"
            t.steps.append({"stage": i + 1, "kind": spec["kind"], "role": spec.get("payload", {}).get("role"),
                            "column": column, "state": state, "job": job.id if job else None})
            for a in attempts:
                t.jobs.append(a.id)
                if a.started:
                    t.started = min(x for x in (t.started, a.started) if x)
                if a.finished:
                    t.finished = max(t.finished or 0, a.finished)
                for e in (await store.events(a.id))[-LOG_LIMIT:]:
                    t.log(e.get("t"), "agent", _event_text(e))
                review = (a.result or {}).get("review")
                if review:
                    issues = "\n".join(f"- {x}" for x in review.get("issues", [])) or "- no issues"
                    t.log(a.finished, "review", f"Score {review.get('score')}/5: {review.get('summary', '')}\n"
                                                f"{issues}")
                if spec["kind"] == "task_fix":
                    found = ((a.payload.get("_carry") or {}).get("review") or {}).get("issues") or []
                    if found:
                        t.details = "Bugs reported by QA:\n" + "\n".join(f"- {x}" for x in found)
                if spec["kind"] == "project_gate" and a.result and a.result.get("answer"):
                    t.details = str(a.result["answer"])
            _merge_status(t)
            if kind == "approval":
                _approval(t, project, job, root is phased[-1][0])
        if phase == "proposal" and project.feedback and version == project.version:
            t = tickets.get(f"proposal-v{version}")
            if t and not t.details:
                t.details = f"Owner feedback for v{version}:\n{project.feedback}"


def _merge_status(t: Ticket) -> None:
    cols = [s["column"] for s in t.steps]
    if "failed" in cols:
        t.column, t.state = "failed", next(s["state"] for s in t.steps if s["column"] == "failed")
    elif "progress" in cols:
        t.column, t.state = "progress", next(s["state"] for s in t.steps if s["column"] == "progress")
    elif cols and all(c == "done" for c in cols):
        t.column, t.state = "done", ""
    elif "done" in cols:
        t.column, t.state = "progress", "running"  # some steps done, the next not started yet
    else:
        t.column, t.state = "todo", ""
    if t.column in {"todo", "progress"}:
        t.finished = None


def _approval(t: Ticket, project: Project, gate: Job | None, latest: bool) -> None:
    """A gate is waiting for the owner until a decision is recorded after it."""
    if gate is None or gate.status is not Status.DONE:
        return
    t.finished = gate.finished
    asked = gate.finished or 0
    decided = [e for e in project.history if e.event in DECISIONS
               and datetime.fromisoformat(e.at).timestamp() >= asked - 5]
    if decided:
        d = decided[0]
        when = datetime.fromisoformat(d.at).timestamp()
        t.column, t.state, t.finished = "done", DECISIONS[d.event], when
        t.log(when, "decision", f"Owner: {DECISIONS[d.event]}" + (f"\n{d.note}" if d.note else ""))
    elif latest and project.state in {"awaiting_go", "awaiting_verification"}:
        t.column, t.state, t.finished = "approval", "waiting", None
    else:
        t.column, t.state = "done", "superseded"


def _commits(folder: Path) -> list[tuple[float, str, str]]:
    try:
        out = subprocess.run(["git", "-C", str(folder), "log", "--format=%ct%x09%h%x09%s", "-n", "2000"],
                             capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        ct, _, rest = line.partition("\t")
        sha, _, subject = rest.partition("\t")
        if ct.isdigit():
            rows.append((float(ct), sha, subject))
    return sorted(rows)


def _progress_entries(folder: Path) -> list[tuple[float | None, str]]:
    path = folder / ".law" / "PROGRESS.md"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out: list[tuple[float | None, str]] = []
    for chunk in re.split(r"\n(?=## )", text):
        if not chunk.startswith("## "):
            continue
        m = STAMP.search(chunk.splitlines()[0])
        t = datetime.strptime(f"{m.group(1)} {m.group(2) or '00:00'}", "%Y-%m-%d %H:%M").timestamp() if m else None
        out.append((t, chunk.strip()))
    return out


def plan_tickets(text: str) -> list[dict[str, Any]]:
    """Development tickets in a PLAN.md build section: id, title, done, milestone and the indented details."""
    out: list[dict[str, Any]] = []
    milestone = ""
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        head = MILESTONE.match(line)
        if head:
            milestone, current = head.group(1), None
            continue
        m = TICKET.match(line)
        if m:
            current = {"id": m.group(3), "title": m.group(4).strip(), "done": m.group(2) != " ",
                       "milestone": milestone, "indent": len(m.group(1)), "details": []}
            out.append(current)
            continue
        if not line.strip():
            continue
        if current is not None and len(line) - len(line.lstrip()) > current["indent"]:
            current["details"].append(line.strip())
        else:
            current = None  # e.g. a "Milestone gate:" line ends the ticket
    for t in out:
        full = " ".join([t["title"], *t.pop("details")]).strip()
        t["title"] = re.split(r"\s*\bAcceptance\b", full)[0].strip(" .;:")
        t["details"] = full
        t.pop("indent")
    return out


async def _plan_tickets(store: JobStore, project: Project, tickets: dict[str, Ticket],
                        dev_jobs: dict[int, list[Job]], dev_order: dict[int, float]) -> None:
    from law.handlers.tasks import build_section

    folder = Path(project.dir)
    commits = _commits(folder)
    progress = _progress_entries(folder)
    # the current version's tickets show even before (or long after, once job history is pruned) its sessions run
    after = max((t.order for t in tickets.values()), default=0) + 1
    for version in sorted({*dev_jobs, project.version}):
        jobs = sorted(dev_jobs.get(version, []), key=lambda j: j.started or j.created)
        section = build_section(folder, version)
        if not section:
            continue
        events = []
        for j in jobs:
            events += [e for e in await store.events(j.id) if e.get("event") == "progress"]
        live = [j for j in jobs if j.status in LIVE]
        stopped = bool(jobs) and not live and jobs[-1].status in STOPPED
        clock = min((j.started for j in jobs if j.started), default=None)
        first_open = True
        for n, item in enumerate(plan_tickets(section)):
            tid = item["id"]
            t = tickets[tid] = Ticket(tid, item["title"], "dev", item["milestone"] or f"Build v{version}",
                                      dev_order.get(version, after) + n / 1000)
            t.details = item["details"]
            mine = [c for c in commits if re.match(rf"{re.escape(tid)}\b", c[2])] or \
                   [c for c in commits if re.search(rf"\b{re.escape(tid)}\b", c[2])]
            for ct, sha, subject in mine:
                t.log(ct, "commit", f"{sha} {subject}")
            for pt, entry in progress:
                if re.search(rf"\b{re.escape(tid)}\b", entry):
                    t.log(pt, "progress", entry[:4000])
            t.started = clock
            if item["done"]:
                t.column = "done"
                t.finished = mine[-1][0] if mine else None
            elif first_open and live:
                t.column, t.state = "progress", live[-1].status.value
                t.jobs = [live[-1].id]
            elif first_open and stopped:
                t.column, t.state = "failed", "cancelled" if jobs[-1].status is Status.CANCELLED else "failed"
                t.finished = jobs[-1].finished
                if jobs[-1].error:
                    t.log(jobs[-1].finished, "error", jobs[-1].error[-2000:])
            else:
                t.started = None
            if not item["done"]:
                first_open = False
            end = t.finished if t.column in {"done", "failed"} else (time.time() if t.column == "progress" else None)
            if t.started and end:
                for e in events:
                    if t.started <= e["t"] <= end:
                        t.log(e["t"], "agent", _event_text(e))
            if t.finished:
                clock = t.finished


def _attach_notes(paths: LawPaths, project: Project, tickets: dict[str, Ticket]) -> list[dict[str, Any]]:
    """Each note on its ticket; a note without one goes to the ticket in progress when it was written."""
    general = []
    for note in notes(paths, project.id):
        target = tickets.get(note.ticket) if note.ticket else _active_at(tickets, note.t)
        entry = note.model_dump()
        if target is None:
            general.append(entry)
        else:
            entry["pinned"] = note.ticket is None
            target.notes.append(entry)
    return sorted(general, key=lambda n: n["t"])


def _active_at(tickets: dict[str, Ticket], t: float) -> Ticket | None:
    now = time.time()
    def end(k: Ticket) -> float:
        return k.finished or (now if k.column == "progress" else 0)

    hits = [k for k in tickets.values() if k.started and k.started <= t <= end(k) and k.kind != "approval"]
    if not hits:
        hits = [k for k in tickets.values() if k.column == "approval"]
    # the most specific: a development ticket over a whole stage, then the one started last
    return max(hits, key=lambda k: (k.kind == "dev", k.started or 0), default=None)


BUG_COLUMN: dict[str, tuple[Column, str]] = {"open": ("todo", "open"), "reopened": ("todo", "reopened"),
                                             "fixed": ("progress", "fixed, awaiting QA retest"),
                                             "verified": ("done", "verified by QA")}


def _bug_tickets(project: Project, tickets: dict[str, Ticket]) -> None:
    """A ticket per bug in `.law/BUGS.md`: written by the QA agent and the owner (`/bugfix`), never by hand here."""
    for n, bug in enumerate(bugs.load(Path(project.dir))):
        t = tickets[bug.id] = Ticket(bug.id, bug.title, "bug", "Bugs", 10**9 + n)
        column, state = BUG_COLUMN.get(bug.status, ("todo", bug.status))
        t.column, t.state = column, f"{bug.severity} · {state}"
        t.details = f"Reported by {bug.reporter} on {bug.when}.\n\n{bug.body}".strip()


async def build(paths: LawPaths, store: JobStore, pid: str) -> dict[str, Any]:
    project = Projects(paths).get(pid)
    roots = sorted([j for j in await store.history(1000) if j.parent is None and _belongs(j, project.id)],
                   key=lambda j: j.created)
    tickets: dict[str, Ticket] = {}
    dev_jobs: dict[int, list[Job]] = {}
    dev_order: dict[int, float] = {}
    await _stage_tickets(store, project, roots, tickets, dev_jobs, dev_order)
    await _plan_tickets(store, project, tickets, dev_jobs, dev_order)
    _bug_tickets(project, tickets)
    general = _attach_notes(paths, project, tickets)
    ordered = sorted(tickets.values(), key=lambda k: k.order)
    return {
        "project": {"id": project.id, "name": project.name, "state": project.state, "version": project.version,
                    "dir": project.dir, "summary": project.summary, "stack": project.stack,
                    "live": any(r.status in LIVE for r in roots),
                    "history": [e.model_dump() for e in project.history]},
        "columns": [{"id": c, "label": label} for c, label in COLUMNS],
        "tickets": [t.as_dict() for t in ordered],
        "notes": general,
    }


def summaries(paths: LawPaths) -> list[dict[str, Any]]:
    return [{"id": p.id, "name": p.name, "state": p.state, "version": p.version} for p in Projects(paths).all()]


def _check_folder(paths: LawPaths, folder: Path) -> None:
    real, root = folder.resolve(), paths.projects.resolve()
    if not real.is_relative_to(root) or real == root or paths.repo.resolve().is_relative_to(real):
        raise ValueError(f"{folder} is not a project folder under {paths.projects}; delete it by hand")


def _remove_files(paths: LawPaths, project: Project, code: bool) -> str | None:
    folder = Path(project.dir)
    _notes_file(paths, project.id).unlink(missing_ok=True)
    for name in ("session.json", "NOTES.md"):
        (folder / ".law" / name).unlink(missing_ok=True)
    if not code or not folder.is_dir():
        return None
    paths.trash.mkdir(parents=True, exist_ok=True)
    dest = paths.trash / f"project-{project.id}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(folder), dest)
    return str(dest)


async def delete_project(paths: LawPaths, store: JobStore, pid: str, *, code: bool = False) -> dict[str, Any]:
    """Remove a project from LAW: its board notes, its jobs with their logs, its machine-local session and pending
    notes, and its index entry. With `code`, the project folder moves to LAW's trash (kept for the trash retention,
    then deleted). Refused while one of its jobs runs."""
    import asyncio

    registry = Projects(paths)
    project = registry.get(pid)
    everything = await store.history(5000)
    jobs = [j for j in everything if _belongs(j, project.id)]
    if any(j.status in LIVE for j in jobs if j.parent is None):
        raise ValueError(f"{project.name} has a job running; cancel it first")
    if code:
        await asyncio.to_thread(_check_folder, paths, Path(project.dir))
    ids = {j.id for j in jobs} | {j.id for j in everything if j.parent and j.parent in {k.id for k in jobs}}
    for job_id in ids:
        await store.delete(job_id)
    registry.forget(project.id)
    trashed = await asyncio.to_thread(_remove_files, paths, project, code)
    return {"deleted": project.id, "jobs": len(ids), "trashed": trashed}
