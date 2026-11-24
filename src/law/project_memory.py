"""A project's working memory in `<project>/.law/`, like `.claude/` for Claude Code.

LAW writes the request and the translated brief; the agent keeps the research, plan and progress current as it
works. If an agent session dies halfway (worker restart, crash, stall), the next run reads `.law/` and continues
from the next unchecked step instead of starting over. `session.json` links a job to its OpenCode session so a
restarted job resumes the same session; it is machine-local and git-ignored. Nothing secret belongs in `.law/`.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

FOLDER = ".law"
# Agent-maintained files, seeded once and never overwritten by LAW.
SEEDS = {
    "RESEARCH.md": "# Research\n\nFindings that shape the design: versions checked, options compared, decisions.\n",
    "PLAN.md": "# Plan\n\nNumbered steps with checkboxes. Each step ends at a gate (build, tests, lint).\n",
    "PROGRESS.md": "# Progress\n\nNewest first: what was done, the last gate output, and the exact next step.\n",
}
INSTRUCTIONS = """This project keeps its working memory in {folder}/ (like .claude/). Use it:
- First read {folder}/PROMPT.md (the brief), then {folder}/PROGRESS.md and {folder}/PLAN.md if they have content.
  If earlier work exists, continue from the next unchecked step; do not start over or re-read the whole codebase.
- Before any work, add this task's to-do list to {folder}/PLAN.md under a new "## Task <date> <title>" heading:
  numbered steps with checkboxes (`- [ ]`) and a gate for each, small enough that each finishes in one step. Keep
  everything already in the file: a "## Build version N" section is the project's ticket board. Record findings in
  {folder}/RESEARCH.md.
- After every step, update {folder}/PROGRESS.md: what was done, the gate output, the exact next step. Tick the step
  in {folder}/PLAN.md. Write findings and results to files as you go, never only at the end: your context can be
  summarised at any time, and what is not on disk is lost.
- Write large files in sections: create the file with the first section, then add one section per edit, at most
  about 1,500 words per write. A single huge write can be cut off and lost; a section costs little to redo.
- Never write secrets, keys or passwords into {folder}/."""
RESUME = ("Your previous session in this project was interrupted. Read {folder}/PROGRESS.md and {folder}/PLAN.md "
          "and continue from the next unchecked step. Keep {folder}/PROGRESS.md current, tick steps as you finish "
          "them, and write large files one section at a time (at most about 1,500 words per write).")



# An allow-list: only LAW's memory files are committed, so an agent's scratch file (seen: a private-key keystore
# in .law/ during an HTTPS step, 2026-10-25) never reaches git through `git add .law/`.
COMMITTED = ("PLAN.md", "PROGRESS.md", "RESEARCH.md", "REQUESTS.md", "PROMPT.md", "ENVIRONMENT.md", "env.sh", "BUGS.md",
             "STYLE.md", "project.json", "check.sh")
GITIGNORE = "*\n!.gitignore\n" + "".join(f"!{name}\n" for name in COMMITTED)

DONE_TICKET = re.compile(r"^\s*- \[[xX]\]", re.MULTILINE)
BUILD_SECTION = re.compile(r"^## Build version \d+.*?(?=^#{1,2} |\Z)", re.MULTILINE | re.DOTALL)


class ProjectMemory:
    def __init__(self, project: Path) -> None:
        self.dir = project / FOLDER

    def prepare(self, job_id: str, request: str, brief: str | None) -> None:
        """Create `.law/`, log the request and store the brief the agent works from."""
        self.dir.mkdir(exist_ok=True)
        for name, text in SEEDS.items():
            path = self.dir / name
            if not path.exists():
                path.write_text(text)
        (self.dir / ".gitignore").write_text(GITIGNORE)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        log = self.dir / "REQUESTS.md"
        if f"· job {job_id}\n" not in (log.read_text() if log.exists() else ""):  # once per job, not per stage
            with log.open("a") as f:
                f.write(f"\n## {stamp} · job {job_id}\n\n{request.strip()}\n")
        # rewritten only when the brief changes: a fresh timestamp every stage left the file always modified, and
        # agents spent a turn on `git diff .law/PROMPT.md` 35 times in Pantry (2026-10-26 to -29) to find out why
        prompt, body = self.dir / "PROMPT.md", f"\n\n{(brief or request).strip()}\n"
        if not (prompt.exists() and prompt.read_text().partition("\n")[2] == body[1:]):
            prompt.write_text(f"# Brief (job {job_id}, {stamp}){body}")

    def plan_text(self) -> str:
        plan = self.dir / "PLAN.md"
        return plan.read_text() if plan.exists() else ""

    def keep_build_sections(self, before: str) -> bool:
        """Put back any `## Build version N` section a session removed from PLAN.md: those sections are the Console
        board (seen 2026-10-30: a Pantry task replaced the file with its to-do list and 158 tickets left the board).
        Returns True when something was restored."""
        now = self.plan_text()
        lost = [s for s in BUILD_SECTION.findall(before) if s.partition("\n")[0].strip() not in now]
        if not lost:
            return False
        (self.dir / "PLAN.md").write_text("\n\n".join([now.rstrip(), *(s.rstrip() for s in lost)]) + "\n")
        return True

    def drop_unstarted_builds(self) -> list[str]:
        """Remove the `## Build version N` sections with no ticket done: a plan the owner sent back before any work
        (seen 2026-11-24: a No go added plan v2's tickets beside v1's, so the board held both plans). Returns the
        removed headings. Sections with work done stay: they are the project's history."""
        now = self.plan_text()
        dropped = [s for s in BUILD_SECTION.findall(now) if not DONE_TICKET.search(s)]
        if dropped:
            for section in dropped:
                now = now.replace(section, "")
            (self.dir / "PLAN.md").write_text(re.sub(r"\n{3,}", "\n\n", now).rstrip() + "\n")
        return [s.partition("\n")[0].strip() for s in dropped]

    def next_ticket(self, key: str) -> int:
        """The first free ticket number for `key`, so a new build version never reuses an ID on the board."""
        found = [int(n) for n in re.findall(rf"\b{re.escape(key)}-(\d+)", self.plan_text())]
        return max(found, default=0) + 1

    def add_build_section(self, section: str) -> None:
        heading = section.partition("\n")[0]
        now = self.plan_text()
        if heading not in now:
            (self.dir / "PLAN.md").write_text(f"{now.rstrip()}\n\n{section}")

    def pending_notes(self) -> str:
        """Owner notes no agent has taken yet."""
        path = self.dir / "NOTES.md"
        return path.read_text().strip() if path.exists() else ""

    def instructions(self) -> str:
        return INSTRUCTIONS.format(folder=FOLDER)

    def resume_prompt(self) -> str:
        return RESUME.format(folder=FOLDER)

    def context(self, limit: int = 3000) -> str:
        """The plan and progress so far, for the translator (empty for a new project)."""
        parts = []
        for name in ("PLAN.md", "PROGRESS.md"):
            path = self.dir / name
            if path.exists() and path.read_text() != SEEDS[name]:
                parts.append(f"{FOLDER}/{name}:\n{path.read_text()[:limit]}")
        return "\n\n".join(parts)

    def ensure_progress(self, started: float, role: str, summary: str, edits: list[str]) -> bool:
        """If the agent did not update PROGRESS.md during its step, LAW records the step itself (what the agent
        reported and the files it changed), so progress is never lost. Returns True when LAW wrote the entry."""
        progress = self.dir / "PROGRESS.md"
        if progress.exists() and progress.stat().st_mtime >= started:
            return False
        self.dir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        minutes = max(1, round((time.time() - started) / 60))
        files = "\n".join(f"- {e}" for e in sorted(set(edits))[:40]) or "- (no files changed)"
        text = summary.strip()[-1500:] or "(the agent reported nothing)"
        entry = (f"\n## {stamp} · {role} step, {minutes} min (recorded by LAW: the agent did not update this file)\n\n"
                 f"{text}\n\nFiles changed:\n{files}\n")
        with progress.open("a") as f:
            if not progress.exists() or progress.stat().st_size == 0:
                f.write(SEEDS["PROGRESS.md"])
            f.write(entry)
        return True

    def add_note(self, text: str) -> None:
        """An owner's note for the agent working here (`/note` in Telegram); delivered at its next action."""
        self.dir.mkdir(exist_ok=True)
        with (self.dir / "NOTES.md").open("a") as f:
            f.write(f"{text.strip()}\n\n")

    def add_followup(self, ticket: str, note: str) -> str | None:
        """An owner's note on a finished ticket becomes a new ticket right after the one in progress, so the work
        reaches the plan instead of being skimmed by an agent busy elsewhere (owner 2026-11-23: notes on chapter 1
        while chapter 2 is written). Returns the new ticket id, or None if `ticket` is not a ticked ticket."""
        plan = self.dir / "PLAN.md"
        try:
            lines = plan.read_text().splitlines()
        except OSError:
            return None
        done = re.compile(rf"^\s*- \[[xX]\]\s+{re.escape(ticket)}\b")
        if not any(done.match(line) for line in lines):
            return None
        key = ticket.split("-", 1)[0]
        numbers = [int(m.group(1)) for line in lines for m in [re.match(rf"^\s*- \[[ xX]\]\s+{re.escape(key)}-(\d+)",
                                                                         line)] if m]
        new = f"{key}-{max(numbers, default=0) + 1}"
        text = " ".join(note.split())
        entry = (f"- [ ] {new} Revise {ticket} as the owner noted: {text} (acceptance: the note is addressed, "
                 f"the gate passes)")
        open_at = next((i for i, line in enumerate(lines) if re.match(r"^\s*- \[ \]", line)), None)
        lines.insert(len(lines) if open_at is None else open_at + 1, entry)  # after the ticket in progress
        plan.write_text("\n".join(lines) + "\n")
        return new

    def take_notes(self) -> str | None:
        """Pending owner notes, once: they move to REQUESTS.md (the committed record) when the agent gets them."""
        path = self.dir / "NOTES.md"
        try:
            text = path.read_text().strip()
        except OSError:
            return None
        path.unlink(missing_ok=True)
        if not text:
            return None
        with (self.dir / "REQUESTS.md").open("a") as f:
            f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} · owner note to the agent\n\n{text}\n")
        return text

    def session_for(self, job_id: str) -> str | None:
        """The OpenCode session this job already started, if it was interrupted."""
        data = self._session()
        return str(data["session"]) if data.get("job") == job_id and data.get("session") else None

    def save_session(self, job_id: str, role: str, session: str) -> None:
        if self.session_for(job_id) == session:
            return
        self.dir.mkdir(exist_ok=True)
        doc = {"job": job_id, "role": role, "session": session, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        (self.dir / "session.json").write_text(json.dumps(doc, indent=2) + "\n")

    def end_session(self, job_id: str) -> None:
        """Forget this job's finished session, so its next agent run starts fresh instead of resuming it."""
        if self.session_for(job_id) is not None:
            (self.dir / "session.json").unlink(missing_ok=True)

    def _session(self) -> dict[str, Any]:
        path = self.dir / "session.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


def project_listing(project: Path, limit: int = 60) -> str:
    """Top two levels of a project (no hidden or build folders), so the translator knows what exists."""
    skip = {"node_modules", "target", "dist", "build", ".venv", "__pycache__"}
    lines: list[str] = []
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if len(rel.parts) > 2 or any(p.startswith(".") or p in skip for p in rel.parts):
            continue
        lines.append(f"{rel}{'/' if path.is_dir() else ''}")
        if len(lines) >= limit:
            lines.append("…")
            break
    return "\n".join(lines) or "(empty folder)"
