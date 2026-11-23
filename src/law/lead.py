"""The lead step: a drafted note for a stuck coding agent, sent to the owner for approval (ENHANCE brief E8, ADR-0026).

When a ticket session stalls twice in a row, LAW asks the coder model, acting as lead developer, whether the agent is
stuck and what specific step it is missing (the prompt replayed on 12 real moments on 2026-10-28: detection 8/9, notes
4/9 as good as a human lead's, so it never reaches the agent unreviewed). A note becomes a draft here; Tony sends it to
the owner with Approve / Edit / Drop. Only an approved or edited note goes to the agent, through the owner-note path
(`ProjectMemory.add_note`). No answer within DECIDE_TIMEOUT_S counts as Drop. Every outcome is logged; every
REPORT_EVERY outcomes the approval rate is appended to the repo's PROGRESS.md so the owner can decide later whether
the step may run unattended.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from law.paths import LawPaths
from law.project_memory import ProjectMemory

DECIDE_TIMEOUT_S = 3600
REPORT_EVERY = 10
DECISIONS = ("approved", "edited", "dropped", "expired")

PROMPT = """You are the lead developer watching a coding agent work on one ticket of a project. LAW's guards already
sent it the generic warnings shown in the activity (lines starting with a warning sign). Decide whether the agent is
stuck (not just slow) and whether you know a specific next step it is missing.

The agent is making progress (saving code, running checks, committing, or trying a new idea for under ~30 min):
answer NO NOTE. Kinds of stuck worth a note:
- planning in its head: long reading with no code; tell it what to write first, in which order, checking after each
- the same problem attempted many times: time-box it and give the simplest fallback, recorded as "owner to confirm"
- an approach the environment cannot support (e.g. jsdom has no layout or CSS cascade): say why, what to check instead
- rebuilding something the project already has: point to the existing piece to reuse
- a finished piece still being polished: tick it and commit
- working inside an oversized file (over ~400 lines): put the new work in a new small file
- a fact it cannot know from the code: state it only if you actually know it; never guess

A note is 2 to 5 sentences, addressed to the agent: the ticket, what to stop, one concrete path (prefer reusing what
already works), a fallback, the checks to run, and "commit". No new scope.

Answer with JSON only: {"stuck": true|false, "diagnosis": "<one sentence>", "note": "<the note, or empty>"}"""


@dataclass
class Draft:
    id: str
    project: str
    folder: str
    ticket: str
    job: str
    diagnosis: str
    note: str
    created: float
    status: str = "pending"  # pending, then one of DECISIONS
    sent: bool = False
    decided: float = 0.0
    final_note: str = ""


def context_message(ticket: str, activity: list[str], folder: Path, now: str | None = None) -> str:
    """What the lead sees: the ticket, recent commits, the agent's recent actions and the project's files by size."""
    return (f"Ticket:\n{ticket}\n\nRecent commits:\n{_git(folder, 'log', '--oneline', '-8')}\n\n"
            f"Agent activity, most recent last (LAW's summaries):\n" + "\n".join(activity[-80:]) +
            f"\n\nProject files (bytes, path):\n{_files(folder)}\n\nNow is {now or time.strftime('%H:%M')}.")


def parse(answer: str) -> tuple[bool, str, str]:
    """(stuck, diagnosis, note) from the lead's JSON reply; anything unparsable is "no note"."""
    found = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        data = json.loads(found.group(0)) if found else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    note = str(data.get("note") or "").strip()
    return bool(data.get("stuck")) and bool(note), str(data.get("diagnosis") or "").strip(), note


class Leads:
    """Drafts in ~/LAW/data/lead/<id>.json and the outcome log ~/LAW/data/lead/outcomes.jsonl."""

    def __init__(self, paths: LawPaths) -> None:
        self.paths = paths
        self.dir = paths.data / "lead"

    def add(self, *, project: str, folder: Path, ticket: str, job: str, diagnosis: str, note: str,
            now: float | None = None) -> Draft:
        self.dir.mkdir(parents=True, exist_ok=True)
        draft = Draft(secrets.token_hex(4), project, str(folder), ticket, job, diagnosis, note, now or time.time())
        self._save(draft)
        return draft

    def get(self, draft_id: str) -> Draft | None:
        if not re.fullmatch(r"[0-9a-f]{8}", draft_id):
            return None
        try:
            return Draft(**json.loads((self.dir / f"{draft_id}.json").read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def all(self) -> list[Draft]:
        found = (self.get(f.stem) for f in sorted(self.dir.glob("*.json")))
        return [d for d in found if d is not None]

    def pending_for(self, job: str) -> Draft | None:
        """The job's undecided draft, if any: one stuck moment gets one note at a time."""
        return next((d for d in self.all() if d.job == job and d.status == "pending"), None)

    def unsent(self) -> list[Draft]:
        return [d for d in self.all() if d.status == "pending" and not d.sent]

    def mark_sent(self, draft: Draft) -> None:
        draft.sent = True
        self._save(draft)

    def decide(self, draft_id: str, decision: str, text: str = "", now: float | None = None) -> Draft | None:
        """Approve, edit (with the owner's text) or drop a pending draft. Approved and edited notes go to the agent.
        Returns the decided draft, or None if it is unknown or already decided."""
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r}")
        draft = self.get(draft_id)
        if draft is None or draft.status != "pending":
            return None
        if decision == "edited" and not text.strip():
            raise ValueError("an edited note needs its text")
        draft.status, draft.decided = decision, now or time.time()
        draft.final_note = draft.note if decision == "approved" else text.strip() if decision == "edited" else ""
        if draft.final_note:
            ProjectMemory(Path(draft.folder)).add_note(f"From the lead developer, reviewed by the owner: "
                                                       f"{draft.final_note}")
        self._save(draft)
        self._log(draft)
        return draft

    def expire(self, now: float | None = None) -> list[Draft]:
        """Drafts left undecided for DECIDE_TIMEOUT_S are dropped (a stale note would mislead the agent)."""
        now = now or time.time()
        late = [d for d in self.all() if d.status == "pending" and now - d.created >= DECIDE_TIMEOUT_S]
        return [d for d in (self.decide(x.id, "expired", now=now) for x in late) if d is not None]

    def outcomes(self) -> list[dict[str, Any]]:
        try:
            lines = (self.dir / "outcomes.jsonl").read_text().splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def _save(self, draft: Draft) -> None:
        path = self.dir / f"{draft.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(draft), indent=1))
        tmp.replace(path)

    def _log(self, draft: Draft) -> None:
        row = {"id": draft.id, "project": draft.project, "ticket": draft.ticket.split(" ", 2)[:2],
               "decision": draft.status, "seconds": round(draft.decided - draft.created)}
        with (self.dir / "outcomes.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        count = len(self.outcomes())
        if count % REPORT_EVERY == 0:
            self._report(count)

    def _report(self, count: int) -> None:
        progress = self.paths.repo / "PROGRESS.md"
        if progress.exists():
            with progress.open("a") as f:
                f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {approval_summary(self.outcomes())} (ADR-0026)\n")


def approval_summary(outcomes: list[dict[str, Any]]) -> str:
    n = len(outcomes)
    counts = {d: sum(1 for o in outcomes if o["decision"] == d) for d in DECISIONS}
    rate = round(100 * (counts["approved"] + counts["edited"]) / n) if n else 0
    return (f"Lead notes: {n} decided, {rate}% used ({counts['approved']} approved, {counts['edited']} edited, "
            f"{counts['dropped']} dropped, {counts['expired']} unanswered)")


def message(draft: Draft) -> str:
    """The owner's Telegram card for a draft (Markdown)."""
    return (f"🧭 **Lead note for {draft.project}**, ticket {draft.ticket[:120]}\n\n"
            f"Diagnosis: {draft.diagnosis}\n\nDraft note to the agent:\n> {draft.note}\n\n"
            f"**Approve** sends it as is, **Edit** lets you reply with your own text, **Drop** discards it. "
            f"No answer in {DECIDE_TIMEOUT_S // 60} min drops it.")


def _git(folder: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "(no git history)"
    return out.stdout.strip() or "(no commits)"


def _files(folder: Path, limit: int = 60) -> str:
    """Tracked and new files, largest first (the 400-line rule needs sizes)."""
    listed = _git(folder, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    sized = []
    for name in listed:
        path = folder / name
        if path.is_file() and not name.startswith(".law/"):
            sized.append((path.stat().st_size, name))
    return "\n".join(f"{size} {name}" for size, name in sorted(sized, reverse=True)[:limit]) or "(none)"
