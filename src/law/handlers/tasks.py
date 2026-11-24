"""Search and multi-step task handlers (OpenCode runs the coding and research agents)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from law import bugs, deliver, lead, specs
from law.handlers.base import Runtime, Stalled, Steered, run_cmd
from law.handlers.core import PASS_SCORE, answer, parse_verdict
from law.lessons import INSTRUCTION as LESSON_RULE
from law.lessons import Lessons
from law.project_memory import FOLDER, ProjectMemory, project_listing
from law.projects import Projects
from law.queue.worker import JOB_TIMEOUT_S, Context, Done, Fatal, Outcome, Stages

# Roles whose output is code or files get a translated brief, an independent review and one fix pass by default.
REVIEWED_ROLES = {"coder", "writer", "designer", "architect", "analyst"}

TRANSLATE = """Write the brief for a {role} agent.

The user's REQUEST:
{request}

Project folder: {folder}
Files there now:
{listing}

Plan and progress recorded so far:
{memory}{fixed}{choose}"""

# A pipeline (e.g. /proposal) that fixes the deliverables and structure: the brief must work within them.
FIXED = """

The agent will ALSO receive these fixed instructions from the pipeline, word for word. Your brief must not
contradict them or name other deliverables, files or structure; concentrate on the request's content (scope,
users, requirements, constraints, acceptance criteria) and leave format and file names to these instructions:
{spec}"""

# A coder request without a folder: the translator names the project folder; LAW validates and creates it, so
# every project gets its own folder (never ~/SourceCode itself, never an unrelated project).
CHOOSE_FOLDER = """

Existing projects in {root}: {projects}
Begin your reply with one line `Project folder: {root}/<name>`. Use an existing project's folder only when the
REQUEST refers to that project by name. Otherwise choose a new, short kebab-case name: the project name the user
gave, or one that describes the project. Then write the brief."""
PROJECT_LINE = re.compile(r"^\W*Project folder:\**\s*`?([^`\s]+)`?\s*$", re.MULTILINE | re.IGNORECASE)
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

NEW_PROJECT_MEMORY = ("Create the project in a new folder ~/SourceCode/<project-name>/, named after the project the "
                      "user named. Never write files directly in ~/SourceCode or into another project's folder.\n"
                      f"In the new project folder, create {FOLDER}/ first: {FOLDER}/PROMPT.md holding the brief "
                      f"below word for word, plus {FOLDER}/RESEARCH.md, {FOLDER}/PLAN.md (numbered steps with "
                      f"checkboxes and gates) and {FOLDER}/PROGRESS.md (done, gate output, next step), and keep "
                      f"them current after every step. Never write secrets into {FOLDER}/.")

CODE_RUBRIC = """You are reviewing work for production use at a bank. Score it 1-5:
5 = correct, complete, clean, well tested, docs accurate; 4 = solid, cosmetic issues only;
3 = works but has a real defect (wrong/misleading docs, dead code, missing edge case, weak tests);
2 = significant bugs or gaps; 1 = broken or missing.
Check: does it do what was asked; correctness and edge cases; tests meaningful; docstrings and README match the
code; no dead code; types and error handling; simplicity: no over-engineering (layers, generic helpers, patterns
or options with one use), simple tasks written simply, and no source or test file over about 400 lines (each is an
issue with a concrete split or simplification). Run the project's checks if you can read how.
Reply with JSON only: {"score": <1-5>, "issues": ["<file:line problem and fix>"], "summary": "<one sentence>"}"""


async def searx(rt: Runtime, query: str, limit: int = 8) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{rt.reg.ports.searxng}/search"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"q": query, "format": "json"})
        r.raise_for_status()
        results = r.json().get("results", [])[:limit]
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in results]


TOOL_ICONS = {"write": "✏️ writing", "edit": "✏️ editing", "patch": "✏️ patching", "read": "📖 reading",
              "bash": "▶ running", "shell": "▶ running", "grep": "🔎 searching", "glob": "🔎 finding",
              "list": "📂 listing", "webfetch": "🌐 fetching", "todowrite": "🗒 planning"}


def project_folder(brief: str, root: Path) -> Path | None:
    """The folder the translator chose: a valid name directly under the projects root, or None."""
    match = PROJECT_LINE.search(brief)
    if not match:
        return None
    path = Path(match.group(1)).expanduser()
    if not path.is_absolute():
        path = root / path
    name = path.name.lower()
    if path.parent.resolve() != root.resolve() or not PROJECT_NAME.match(name):
        return None
    return root / name


def project_env(project: Path) -> dict[str, str]:
    """Variables `<project>/.law/env.sh` sets or changes (e.g. JAVA_HOME, PATH), written by the environment stage."""
    import os
    import subprocess

    script = project / ".law" / "env.sh"
    if not script.is_file():
        return {}
    out = subprocess.run(["bash", "-c", 'set -a; source "$1" >/dev/null 2>&1; env -0', "_", str(script)],
                         capture_output=True, timeout=30, cwd=project)
    if out.returncode != 0:
        return {}
    pairs = (item.split("=", 1) for item in out.stdout.decode(errors="replace").split("\0") if "=" in item)
    return {k: v for k, v in pairs if os.environ.get(k) != v and k not in {"_", "SHLVL", "PWD", "OLDPWD"}}


def build_section(project: Path, version: int) -> str | None:
    """The `## Build version N` section of .law/PLAN.md (the development milestones), or None."""
    plan = project / ".law" / "PLAN.md"
    text = plan.read_text() if plan.exists() else ""
    head = re.search(rf"^(#+)\s*Build version {version}\b.*$", text, re.IGNORECASE | re.MULTILINE)
    if not head:
        return None
    level = len(head.group(1))
    nxt = re.compile(rf"^#{{1,{level}}}\s", re.MULTILINE).search(text, head.end())
    return text[head.end():nxt.start() if nxt else len(text)]


ID_LINE = re.compile(r"^(\s*- \[)([ xX])(\]\s*)([A-Z][A-Z0-9]*-\d+(?:\.\d+)*)\b")


def tick_split_parents(project: Path) -> list[str]:
    """Tick every ticket whose sub-tickets (P-34.1, P-34.2...) are all ticked; returns the IDs ticked. Seen
    2026-10-29: P-34.4's and P-34's own lines stayed open after their work was committed, and each cost a session
    that only rechecked finished work."""
    plan = project / FOLDER / "PLAN.md"
    if not plan.exists():
        return []
    lines = plan.read_text().splitlines(keepends=True)
    ticked: list[str] = []
    while True:
        state = {m.group(4): m.group(2) != " " for line in lines if (m := ID_LINE.match(line))}
        done = [tid for tid, closed in state.items() if not closed and
                (parts := [c for t, c in state.items() if t.startswith(f"{tid}.")]) and all(parts)]
        if not done:
            break
        for n, line in enumerate(lines):
            if (m := ID_LINE.match(line)) and m.group(4) in done:
                lines[n] = f"{m.group(1)}x{m.group(3)}{line[m.end(3):]}"
        ticked += done
    if ticked:
        plan.write_text("".join(lines))
    return ticked


def plan_complete(project: Path, version: int) -> bool:
    """True when this version's build section exists and every milestone in it is ticked. Other checklists in
    PLAN.md (a proposal's sections, a research plan, another version) do not count."""
    section = build_section(project, version)
    return section is not None and "- [x]" in section.lower() and "- [ ]" not in section


# Markdown allows a heading and a top-level list item to start with a space (seen 2026-10-27: an agent indented a
# whole new milestone by one space, which hid its tickets); a ticket's details are indented two or more.
MILESTONE_HEAD = re.compile(r"^ {0,3}#{3,}\s+(.+?)\s*$")
OPEN_TICKET = re.compile(r"^ ?- \[ \]\s*(.+?)\s*$")
# A line with a ticket ID is a ticket at any indent (seen 2026-10-28: split tickets P-37.2-.5 indented two spaces).
TICKET_ID = re.compile(r"[A-Z][A-Z0-9]*-\d+(?:\.\d+)*")
EST_TAG = re.compile(r"\s*\[\s*est[^\]]*\]", re.IGNORECASE)
OPEN_ID_TICKET = re.compile(r"^\s*- \[ \]\s*([A-Z][A-Z0-9]*-\d+(?:\.\d+)?\b.*?)\s*$")


def uncommitted(project: Path) -> list[str]:
    """Product files with uncommitted changes (LAW's own `.law/` excluded)."""
    import subprocess

    out = subprocess.run(["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True, timeout=30)
    return [ln[3:] for ln in out.stdout.splitlines() if ln[3:] and not ln[3:].startswith(f"{FOLDER}/")]


AUTO_COMMIT_MAX_FILES = 300  # more than this in one ticket is a build output or dependency folder, not work
AUTO_COMMIT_MAX_MB = 20


def _git(project: Path, *args: str) -> str:
    import subprocess

    res = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()[-400:]}")
    return res.stdout


def head(project: Path) -> str:
    try:
        return _git(project, "rev-parse", "HEAD").strip()
    except RuntimeError:
        return ""  # no commit yet


def tick(project: Path, tid: str) -> bool:
    """Tick ticket `tid` in .law/PLAN.md; False when it is not an open ticket there."""
    plan = project / FOLDER / "PLAN.md"
    if not plan.is_file():
        return False
    text = plan.read_text()
    new, n = re.subn(rf"(?m)^(\s*- )\[ \](\s*{re.escape(tid)}\b)", r"\1[x]\2", text, count=1)
    if n:
        plan.write_text(new)
    return bool(n)


def record_progress(project: Path, title: str, reply: str) -> None:
    """Add the ticket's entry at the top of .law/PROGRESS.md (newest first)."""
    progress = project / FOLDER / "PROGRESS.md"
    text = progress.read_text() if progress.is_file() else "# Progress\n"
    body = reply.strip()[-1500:] or "(no summary from the agent)"
    entry = f"## {time.strftime('%Y-%m-%d %H:%M')} {title}\n\n{body}\n\n.law/check.sh passed (run by LAW).\n\n"
    at = text.find("\n## ")
    progress.write_text(text[:at + 1] + entry + text[at + 1:] if at >= 0 else text.rstrip() + "\n\n" + entry)


def finish_ticket(project: Path, ticket: str, reply: str, before: str) -> str:
    """After a session whose gate passed: commit its work as the ticket, tick it and record its progress.
    Returns "" when the ticket is done, or the problem the next session must fix."""
    m = TICKET_ID.match(ticket)
    if m is None:
        return ""
    tid = m.group(0)
    title = re.split(r"\s*\(acceptance", ticket, flags=re.IGNORECASE)[0].strip()
    title = EST_TAG.sub("", title).strip()
    status = [ln for ln in _git(project, "status", "--porcelain", "--untracked-files=all").splitlines() if ln[3:]]
    if not status and head(project) == before:
        return (f"The session for {tid} changed nothing in the project. Do the ticket's work (files and tests), "
                "with .law/check.sh passing, then stop with your summary.")
    big = []
    for line in status:
        path = project / line[3:].strip().strip('"')
        if path.is_file() and path.stat().st_size > AUTO_COMMIT_MAX_MB * 1_000_000:
            big.append(line[3:])
    if len(status) > AUTO_COMMIT_MAX_FILES or big:
        what = ", ".join(big[:5]) if big else f"{len(status)} files"
        return (f"LAW did not commit {tid}: the change has {what}, which looks like build output, downloads or "
                "dependencies, not the ticket's work. Add those paths to .gitignore (or delete them), keep "
                ".law/check.sh passing, then stop with your summary.")
    tick(project, tid)
    record_progress(project, title, reply)
    _git(project, "add", "-A")
    if _git(project, "status", "--porcelain"):
        _git(project, "commit", "-q", "-m", f"{title}\n\n{reply.strip()[-1500:]}".strip())
    return ""


def changed_since(project: Path, since: float, *, code_only: bool = False) -> bool:
    """A file the agent works on (tracked or new, LAW's `.law/` included unless `code_only`) was modified after
    `since` (epoch)."""
    import subprocess

    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=project,
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in out.splitlines():
        name = line[3:].strip().strip('"')
        if code_only and name.startswith(f"{FOLDER}/"):
            continue
        path = project / name
        try:
            if path.is_file() and path.stat().st_mtime > since:
                return True
        except OSError:
            continue
    return False


def is_split_of(part: str, whole: str) -> bool:
    """Whether ticket line `part` is a sub-ticket of ticket line `whole` (P-29.1 of P-29). Seen 2026-10-29: the agent
    split P-29 in its plan; LAW took the changed next ticket for "done" and ended a session that had just read 25
    minutes for that work."""
    a, b = TICKET_ID.match(part), TICKET_ID.match(whole)
    return a is not None and b is not None and a.group(0).startswith(f"{b.group(0)}.")


def next_ticket(project: Path, version: int) -> tuple[str, str] | None:
    """The first unticked ticket of this version's build: (its milestone heading, the ticket line), or None. A ticket
    split into unticked sub-tickets (P-34 into P-34.1, P-34.2...) gives way to them, so each gets its own session."""
    milestone, found = "", []
    for line in (build_section(project, version) or "").splitlines():
        if head := MILESTONE_HEAD.match(line):
            milestone = head.group(1)
        elif ticket := OPEN_TICKET.match(line) or OPEN_ID_TICKET.match(line):
            found.append((milestone, ticket.group(1)))
    ids = [TICKET_ID.match(text) for _, text in found]
    for (milestone, text), tid in zip(found, ids, strict=True):
        if tid is None or not any(o is not None and o.group(0).startswith(f"{tid.group(0)}.") for o in ids):
            return milestone, text
    return None


# Development runs one fresh agent session per ticket (ADR-0019): measured on the 27B coder, replies grow with the
# context (median 150 output tokens below a 15K prompt, 702 above 70K; p90 1.3K -> 6.6K), and a milestone-long
# session reached 70K+ and was compacted. After each ticket LAW runs the project's gate itself.
TICKETS_PER_STAGE = 20  # a stage still ends with its milestone; this only bounds a runaway loop
IDLE_SESSIONS = 2  # sessions in a row that tick nothing and fix no failing gate end the stage, as before
# A stage is one job run, which the worker stops at JOB_TIMEOUT_S (seen 2026-10-26: a milestone stage failed at 6 h
# and used up a retry). It starts no ticket after STAGE_BUDGET_S, caps each session to the time left, and ends cleanly
# when one runs out: the next stage continues the milestone in a fresh job.
STAGE_BUDGET_S = 4 * 3600
STAGE_MARGIN_S = 10 * 60
MIN_SESSION_S = 30 * 60
CHECK_SCRIPT = f"{FOLDER}/check.sh"
CHECK_TIMEOUT_S = 30 * 60
CHECK_TAIL = 3000
# LAW does a ticket's bookkeeping (2026-11-24): agents spent over half of their model turns editing PLAN.md and
# PROGRESS.md and running git, at 15-45 s a turn on the local model. LAW already runs the gate after every ticket;
# when it passes, LAW commits the work with the ticket's ID, ticks it and records the agent's reply as its progress.
TICKET_SESSION = ("This session: ticket {ticket} (milestone {milestone}). Do only this ticket: its work and tests, "
                  "with .law/check.sh passing. Then stop and reply in a few lines: what you did, any decision or "
                  "gotcha the next session needs, anything the owner must confirm by hand, and the check result. "
                  "LAW then runs .law/check.sh itself, commits your work as the ticket, ticks it on the board and "
                  "records your reply in .law/PROGRESS.md: do not commit, tick the ticket or edit .law/PROGRESS.md "
                  "(that replaces the bookkeeping steps described above). Split a ticket that turns out too big in "
                  ".law/PLAN.md as described, then do its first part.")
# A ticket is a story-sized change: the session scans the project for what exists (no duplicates), then reads only
# what the ticket touches. Seen 2026-10-27: every Pantry session spent 25-75 min re-reading the codebase first.
TARGETED = ("Work like a developer picking up one story: do not read the whole codebase. The project map below lists "
            "every file with its size. Scan, don't read: use the map and `rg <name>` to check whether what this "
            "ticket needs already exists (reuse it, never build it twice); then open only the files this ticket "
            "changes and their tests, and treat the rest as new work.\n\nProject map (lines, file):\n{map}")
MAP_LIMIT = 200
SOURCE_FILE = re.compile(r"\.(?:java|kt|kts|ts|tsx|js|jsx|mjs|py|go|swift|rs|sql|css|scss|html|vue|svelte|sh|ya?ml|"
                         r"json|xml|toml|md)$")


def project_map(project: Path) -> str:
    """Tracked source, test and config files with their line counts (no lock files, assets or LAW's memory)."""
    import subprocess

    out = subprocess.run(["git", "ls-files"], cwd=project, capture_output=True, text=True, timeout=30).stdout
    files = [f for f in out.splitlines() if SOURCE_FILE.search(f) and not f.startswith((f"{FOLDER}/", "docs/"))
             and not f.endswith(("package-lock.json", "pnpm-lock.yaml", "yarn.lock"))]
    rows = []
    for name in files[:MAP_LIMIT]:
        try:
            lines = (project / name).read_text(errors="replace").count("\n")
        except OSError:
            continue
        rows.append(f"{lines:>5} {name}")
    if len(files) > MAP_LIMIT:
        rows.append(f"  ... {len(files) - MAP_LIMIT} more (git ls-files)")
    return "\n".join(rows) or "(no files yet)"


# A ticket session used to open .law/PROMPT.md, PLAN.md and PROGRESS.md (and STYLE.md for a book) before any work:
# 5-7 minutes a ticket on the local model (2026-11-24 trial; 10 % of all agent time measured across jobs), and
# PROGRESS.md grows with every ticket. LAW gives their current, relevant part instead.
CONTEXT_HEAD = ("LAW's copy of what you need from {folder}/ (current as of this session; do not open PROMPT.md, "
                "PLAN.md, PROGRESS.md or STYLE.md again unless the ticket needs something older than this):")
PROGRESS_ENTRIES = 2
CONTEXT_CHARS = {"PROMPT.md": 6000, "STYLE.md": 4000, "PROGRESS.md": 3000}


def _head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n[... cut]"


def milestone_board(project: Path, version: int, milestone: str) -> str:
    """The ticket's milestone from the board: its heading and every ticket line with its tick."""
    section = build_section(project, version) or ""
    lines, inside = [], False
    for line in section.splitlines():
        if head := MILESTONE_HEAD.match(line):
            inside = head.group(1) == milestone
            if inside:
                lines.append(line)
            continue
        if inside and line.strip():
            lines.append(line)
    return "\n".join(lines)


def ticket_context(project: Path, version: int, milestone: str) -> str:
    """The brief, the ticket's milestone, the latest progress entries and (for a book) the style guide."""
    law = project / FOLDER
    parts = [CONTEXT_HEAD.format(folder=FOLDER)]

    def read(name: str) -> str:
        path = law / name
        return path.read_text(errors="replace").strip() if path.is_file() else ""

    if brief := read("PROMPT.md"):
        parts.append(f"--- {FOLDER}/PROMPT.md (the agreed brief) ---\n{_head(brief, CONTEXT_CHARS['PROMPT.md'])}")
    if board := milestone_board(project, version, milestone):
        parts.append(f"--- {FOLDER}/PLAN.md, this milestone ---\n{board}")
    progress = read("PROGRESS.md")
    entries = re.split(r"(?m)^(?=## )", progress)
    latest = "".join([e for e in entries if e.startswith("## ")][:PROGRESS_ENTRIES])  # newest first
    if latest.strip():
        parts.append(f"--- {FOLDER}/PROGRESS.md, latest {PROGRESS_ENTRIES} entries ---\n"
                     f"{_head(latest.strip(), CONTEXT_CHARS['PROGRESS.md'])}")
    if style := read("STYLE.md"):
        parts.append(f"--- {FOLDER}/STYLE.md ---\n{_head(style, CONTEXT_CHARS['STYLE.md'])}")
    return "\n\n".join(parts) if len(parts) > 1 else ""


FIRST_TICKET = ("This session: .law/PLAN.md has no build section for this version yet. Write it as described "
                "above, then do its first ticket (code and tests, passing) and stop with a short reply: LAW commits "
                "and ticks it.")
ALL_TICKED = "Every ticket is ticked: fix the problem above, commit, and stop."
# OpenCode continues a session on its own after it compacts one (seen 2026-10-27: P-22.4's session went on to
# P-22.5, so LAW never checked P-22.4). LAW ends a session once its ticket is ticked and committed.
TICKET_DONE = "ticket done"
GATE_FAILED = ("LAW checked the last ticket and it is not done:\n\n{problem}\n\nFix this first (open only the files "
               "the output names), run .law/check.sh until it passes, and commit the fix with the ticket's ID. Then "
               "carry on with the ticket below.\n\n")


def describe_opencode_line(line: str) -> str | None:
    """Turn one `opencode run --format json` event into a short, readable progress line (or None to skip)."""
    if not line.startswith("{"):
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    part = ev.get("part") or {}
    if part.get("type") != "tool" or (part.get("state") or {}).get("status") != "completed":
        return None
    tool = str(part.get("tool", ""))
    args = (part.get("state") or {}).get("input") or {}
    target = args.get("filePath") or args.get("command") or args.get("pattern") or args.get("path") or args.get("url")
    if tool.startswith("mac_"):
        return f"🖥 {tool[4:].replace('_', ' ')}"
    if tool == "webfetch" and target:  # the site only, so chat apps don't render the URL as a link card
        host = re.sub(r"^\w+://", "", str(target)).split("/", 1)[0]
        return f"🌐 reading {'web search' if host.startswith(('127.0.0.1', 'localhost')) else host}"
    label = TOOL_ICONS.get(tool, f"🔧 {tool}")
    if not target:
        return label
    lines = [ln.strip() for ln in str(target).replace(str(Path.home()), "~").splitlines() if ln.strip()]
    if tool in ("bash", "shell"):  # (shell in OpenCode 2) what ran, not the boilerplate around it (every command
        # starts with sourcing env.sh)
        heredoc = next((i for i, ln in enumerate(lines) if "<<" in ln), None)  # after it: data, not commands
        lines = lines[:heredoc + 1] if heredoc is not None else lines
        meaningful = [BANNER.sub("", BOILERPLATE_PREFIX.sub("", ln)).strip() for ln in lines
                      if not BOILERPLATE_LINE.match(ln)]
        lines = [ln for ln in meaningful if ln] or lines
        return f"{label} {' ; '.join(lines)[:140]}"
    text = lines[0] if lines else ""
    return f"{label} {text[:140]}"


BOILERPLATE_LINE = re.compile(r"^(?:source \S*env\.sh|cd \S+|echo [\"']?=+[^;&|]*[\"']?)\s*;?\s*$")
BANNER = re.compile(r"echo\s+([\"'])=+[^\"']*\1\s*(?:;|&&)\s*")  # echo "=== X ===";  between commands
BOILERPLATE_PREFIX = re.compile(r"^(?:(?:source \S*env\.sh|cd \S+)\s*(?:&&|;)\s*)+")


# An agent that changes no file for this long is stuck (seen: a 2h20m re-read loop across context compactions).
# It is stopped and its session resumed with a nudge to build and fix, at most STALL_NUDGES times.
STALL_MINUTES = 25
EMPTY_REVIEW_CHARS = 400  # a review shorter than this has no findings to state a verdict from
OWNER_NOTE_HEAD = "A note from the owner"
OWNER_NOTE = (OWNER_NOTE_HEAD + " while you work (from Telegram):\n\n{note}\n\nTake it into account, then carry on "
              "with the current step.")
STALL_NUDGES = 3
DB_LOCKED = "database is locked"
DB_LOCKED_WAITS_S = (10, 30, 90)
NUDGE = ("You have not changed any file for {minutes} minutes. Stop reading and exploring. If the plan or the "
         "project does not exist yet, write it now: the build section of .law/PLAN.md, then the first ticket's "
         "files. Otherwise run the project's build or tests now, fix the first error they report (open only the "
         "file it names), and repeat. "
         "If the error is a library class or method that does not exist (cannot find symbol, does not override), "
         "run `law api <Class> [member]` once for the real signatures instead of guessing again. "
         "Commit when a gate passes.")
# A ticket session nudged twice in a row without saving anything is too big to start (seen 2026-10-28: P-22 and P-33
# planned their whole ticket in reasoning for hours; splitting P-22 by hand unblocked it).
SPLIT_NUDGE = ("Twice now you have saved nothing for {minutes} minutes: this ticket is too big to start in one step. "
               "Split it now in .law/PLAN.md into sub-tickets <ID>.1, <ID>.2, ... replacing its line, each one layer "
               "or one file (e.g. migration; entity and repository; service logic and its tests; API and its tests), "
               "then write the first sub-ticket at once, run the checks, commit it and tick it.")
# A ticket session that stalls with its work done but uncommitted (seen 2026-10-28/29: the P-35 spec was complete but
# kept being polished; P-34.6's test and gate passed, then 35 min of rereading the plan): LAW runs the gate itself.
COMMIT_NUDGE = ("LAW ran .law/check.sh: it passes, with your changes to {files}. If the ticket is complete, stop "
                "now and reply with your summary: LAW commits and ticks it. If something in the ticket is still "
                "missing, do that first.")
UNCOMMITTED_HEAD = "Uncommitted changes were left behind: "  # the gate passed; only the commit is missing
# Writers, researchers and designers produce documents: the nudge is to save work, a section at a time.
NUDGE_DOCS = ("You have not saved anything for {minutes} minutes. Save your work now: write the next section of the "
              "document to its file (at most about 1,500 words per write, appending section by section), tick it in "
              ".law/PLAN.md, and continue the same way. Do not write the whole document in one step.")
# Reading library internals (decompiling jars, hunting jars on disk) to learn an API: seen in four coder runs,
# costing hours each. The sanctioned way is one `law api` call (law.apilookup). After LIB_DIG_LIMIT such commands
# in one step the agent is stopped and resumed with LIB_NUDGE.
LIB_DIG_LIMIT = 3


def reads_law_internals(summary: str, repo: Path, home: Path) -> bool:
    """An agent reading LAW's own source or its state folder (secrets): seen 2026-10-26, a coder reading LAW's
    cli.py, deliver.py and paths.py to learn how `law send` finds the Telegram token."""
    return str(repo / "src" / "law") in summary.replace("~", str(Path.home())) or \
        str(home / "state") in summary.replace("~", str(Path.home()))


READ_LIMIT = 20  # files read or searched since the last save (seen: every ticket opened with 25 min of reading)
READ_NUDGE = ("You have read enough to start. Write the change for the current ticket now (code and its test), run "
              "the checks, and read more only where the output points.")
# The same file opened again and again (seen 2026-10-28: P-33 read the same five files three times in an hour, each
# time after its context was compacted). A file edited since counts afresh: reading it back to check is fine.
REREAD_LIMIT = 3
REREAD_NUDGE = ("You have opened {name} {times} times this session. You know what it says: write the change now, "
                "in the smallest next step (one file), run the checks, and read again only where the output points.")
BLIND_EDIT_LIMIT = 15  # saved files in a row without a build or test run (seen: 1.5 h of test edits, none run)
RUNS_CHECKS = re.compile(r"\b(?:npm (?:run )?(?:test|build)|npx (?:vitest|tsc|playwright)|vitest|tsc\b|mvn\b|gradle|"
                         r"pytest|go (?:test|build|vet)|cargo (?:test|build)|xcodebuild|swift (?:test|build)|monkeyc|"
                         r"just check|make\b)")
# Throwaway probe files mark a debugging loop (seen 2026-10-26 to -28: every stuck episode wrote them, e.g. five
# scratch.probe.test.tsx writes in P-22, ScratchDebugTests in the 3 h P-24.1 barcode hunt, two in P-34.5).
PROBE_FILE = re.compile(r"(?i)/(?:(?:probe|scratch)[^/]*|[^/]*(?:probe|scratch|debug|dump)[^/]*\.(?:test|spec)\.[jt]sx?"
                        r"|(?:probe|scratch|debug)\w*tests?\.java)$")
# A shell command that writes a file (seen 2026-10-29: P-37.4 and P-34.4 appended to RESEARCH.md and PROGRESS.md
# with `cat >> file <<'EOF'`, against the house standard; such edits bypass the edit events LAW tracks).
SHELL_WRITE = re.compile(r"(?:\bcat\b[^|;&]*>>?\s*\S+[^|;&]*<<|<<-?\s*['\"]?\w+['\"]?[^|;&]*>>?\s*[\w~./-]+|"
                         r"\bsed\s+-i\b|\bperl\s+-\w*i|\btee\s+(?:-a\s+)?[\w~./-]+\s*<<)")
SHELL_WRITE_NUDGE = ("Change files with the edit and write tools, not with shell commands (cat > or >> with a "
                     "heredoc, sed -i, tee). Redo that change with the edit tool if it is not right, then carry on.")
PROBE_LIMIT = 2  # probe writes in one session before LAW calls time
# Writers loop on cosmetic details through numbered test files or files in a temp folder (seen 2026-11-23: a
# one-page checklist PDF was done at minute 13, then 17 test files t1.typ..t17.typ over 80 min chasing a ☐ glyph).
DOC_PROBE = re.compile(r"(?:cat\s*>|tee|✏️ \w+)\s+\S*(?:/(?:tmp|cache/tmp)/\S*|(?<![\w.-])(?:t|test|try|tmp)\d+\.\w+)")
DOC_PROBE_LIMIT = 3
DOC_PROBE_NUDGE = ("You are trying variations in test files. Stop: keep the version that already works, put it in "
                   "the deliverable, delete the test files and finish the remaining steps of your plan. A cosmetic "
                   "detail is not worth more tries.")
WRAP_MINUTES = 45  # a one-off document task (not a project ticket) past this is polishing
WRAP_NUDGE = ("You have worked {minutes} minutes on this task. Finish now with what works: complete the deliverable, "
              "check it once, deliver it and stop. Note anything left as \"could improve: ...\" in your answer.")
PROBE_NUDGE = ("You are writing probe files to debug one problem. Time-box it now: if this approach has not worked "
               "after about 30 minutes or 3 attempts, take the simplest fallback that meets the ticket (a check of "
               "what the code depends on instead of a test the environment cannot run, the nearest passing example, "
               "or a smaller acceptance), write \"owner to confirm: <what and why>\" in .law/PROGRESS.md, delete the "
               "probe files, run the checks and commit.")
RUN_CHECKS_NUDGE = ("You have changed many files without running the build or tests. Run them now (the project's "
                    "build, type check and tests) and let the output guide your next change.")
CODE_FILE = re.compile(r"\.(?:java|kt|kts|scala|ts|tsx|js|jsx|mjs|cjs|py|go|swift|rb|cs|php|rs|sql|vue|svelte)\b")
QA_CODE_NUDGE = ("QA tests the product, not the code: do not read or search the source. Use the requirements, the "
                 "README and the running product (browser, HTTP API, commands), and report what a user sees.")
QA_TASK = """Test {name} version {version}, the product in {folder}, as an independent user would.

Requirements: {folder}/.law/PROMPT.md (the brief), {folder}/{spec_file} (its acceptance criteria) and every feature
or change the owner asked for in {folder}/.law/REQUESTS.md (each one is a requirement). How to run it: the README.
The bug list is {folder}/.law/BUGS.md (statuses: open, fixed, verified, reopened).

0. Run the regression pack first: every check in {folder}/qa/ (how to run them is in {folder}/qa/README.md). A
   check that fails is a bug: reopen its bug, or file a new one.
1. Retest: for every bug that is open, fixed or reopened, repeat its steps. Set its status word in the heading to
   verified if it is gone, or reopened if it is still there, and add a line "Retest <date>: <result>".
2. Then test every requirement and acceptance criterion, and the edges a user hits, on the running product with the
   data already in it as well as your own test data. For each new bug append:
   ## BUG-<next number> · open · QA · <YYYY-MM-DD HH:MM>
   <one-line title of what goes wrong>
   Severity: blocker (a core requirement fails) | major (a requirement works wrongly) | minor (cosmetic, or an edge
   with a workaround)
   Steps: ... Expected: ... Actual: ... Evidence: <output, status code, or a screenshot path under $TMPDIR>
3. Keep the pack: save every check you ran as a script in {folder}/qa/ (named after the requirement or the bug it
   covers) and keep {folder}/qa/README.md saying how to run them all with one command. Checks drive the running
   product only (browser, HTTP, commands), never the source.
4. Reply with JSON only: {{"tested": ["<requirement>: pass|fail"], "summary": "<one sentence>"}}"""


EDITOR_TASK = """Check {name} version {version}, the document in {folder}, as its independent editor and fact-checker.

Requirements: {folder}/.law/PROMPT.md (the brief), {folder}/{spec_file} (the agreed outline), {folder}/.law/STYLE.md
and every change the owner asked for in {folder}/.law/REQUESTS.md. The findings list is {folder}/.law/BUGS.md
(statuses: open, fixed, verified, reopened).

1. Retest: for every finding that is open, fixed or reopened, check it again. Set its status word in the heading to
   verified if it is resolved, or reopened if not, and add a line "Retest <date>: <result>".
2. Run {folder}/.law/check.sh (it builds the PDF and HTML). A failure is a finding.
3. Fact-check every chapter: open the cited source of each factual statement, figure, date and regulation reference
   and confirm it; check terms and numbers agree across chapters and with the glossary, and that every part of the
   outline and every owner request is covered. For each new finding append:
   ## BUG-<next number> · open · editor · <YYYY-MM-DD HH:MM>
   <one-line title of what is wrong>
   Severity: blocker | major | minor
   Where: <file, section, page>  Evidence: <the source's words or the command output>  Correct: <what it should say>
4. Reply with JSON only: {{"tested": ["<chapter or check>: pass|fail"], "summary": "<one sentence>"}}"""
PAGES_TASK = """Check how {name} version {version}, the document in {folder}, looks on the page: you can see images.

1. Build it with {folder}/.law/check.sh, then render every page of the PDF: `pdftoppm -png -r 50 <pdf> $TMPDIR/p`.
2. Look at every page image. A finding is: a diagram, image, table or code block cut off at a page edge or running
   past the margin; text too small to read; an empty or near-empty page; a missing or broken image; a heading
   alone at the bottom of a page. Check a few HTML pages the same way (a headless browser screenshot).
3. Append each finding to {folder}/.law/BUGS.md:
   ## BUG-<next number> · open · editor · <YYYY-MM-DD HH:MM>
   <one-line title>  Severity: major (cut, unreadable or missing) | minor (layout)
   Where: <page number and figure or table>  Evidence: <the page image path under $TMPDIR>
4. Reply with JSON only: {{"screens": ["page <n>: pass|fail"], "summary": "<one sentence>"}}"""

QA_NO_REPORT = ("\n\nAn earlier QA session ended without its report, so nothing was verified. Work through the steps "
                "above in order, and end with the JSON report.")
NO_EVIDENCE = ("QA evidence missing: {what}. Nothing here proves the product works; run QA again and read why the "
               "session ended early.")
DESIGN_TASK = """Check how {name} version {version}, the running product in {folder}, looks against its design: you
are the design reviewer, and you can see images. Design: {design}. Owner's requests: {folder}/.law/REQUESTS.md.

1. Start the product as its README says (it may be running already). List the screens and components the design names.
2. Open every screen in a headless browser at a phone viewport (390 x 844), with the product's real data and with one
   item in each state. Save each screenshot under $TMPDIR, then open the image and look at it. Open any reference image
   the same way.
3. Compare what you see, one item at a time: is each screen and component there, and do its layout, density, sizes and
   wording match? Count how many items fit on one screen against the design. Do images show where the design has them?
4. Each screen or component that is missing or clearly different is a bug. Append it to {folder}/.law/BUGS.md as:
   ## BUG-<next number> · open · QA · <YYYY-MM-DD HH:MM>
   <one-line title>
   Severity: major (missing or wrong against the design) | minor (small visual difference)
   Steps: ... Expected: <the design's section> Actual: ... Evidence: <the screenshot path>
5. Reply with JSON only: {{"screens": ["<screen or component>: pass|fail"], "summary": "<one sentence>"}}"""


def design_docs(folder: Path) -> list[str]:
    """The project's design documents and reference images, relative to it."""
    root = folder / "docs" / "design"
    if not root.is_dir():
        return []
    found = (f for f in root.rglob("*") if f.suffix.lower() in {".md", ".png", ".jpg", ".jpeg", ".webp", ".svg"})
    return sorted(str(f.relative_to(folder)) for f in found)


def json_report(reply: str) -> dict[str, Any]:
    found = re.search(r"\{.*\}", reply, re.DOTALL)
    try:
        report = json.loads(found.group(0)) if found else {}
    except json.JSONDecodeError:
        return {}
    return report if isinstance(report, dict) else {}


def qa_pack_kept(folder: Path) -> bool:
    return any(f.is_file() and f.name != "README.md" for f in (folder / "qa").rglob("*"))


def reads_product_code(summary: str, project: Path) -> bool:
    """QA opening or searching the product's source: it tests behaviour, so it must not look (scratch scripts in
    $TMPDIR are its own)."""
    s = summary.replace("~", str(Path.home()))
    if "cache/tmp" in s or "$TMPDIR" in s or "/.law/" in s or f"{project}/qa/" in s or re.search(r"(?:^|\s)qa/", s):
        return False  # its scratch files, the bug list and its own regression pack
    if summary.startswith("📖"):
        return str(project) in s and bool(CODE_FILE.search(s))
    if summary.startswith("🔎"):
        return bool(CODE_FILE.search(s)) or "src/" in s
    if summary.startswith("▶"):
        reads = re.search(r"(?:^|[\s;&|(])(?:cat|head|tail|less|more|sed|awk|rg|grep|find)\s", s)
        return bool(reads) and (bool(CODE_FILE.search(s)) or "/src/" in s or " src/" in s)
    return False


LAW_INTERNALS_NUDGE = ("Do not read LAW's own source code or its state folder (it holds secrets). Software you build "
                       "uses only LAW's supported interfaces, listed here:")


def is_library_dig(command: str) -> bool:
    """A command that inspects library internals: decompilers, jar listings, a search for library jars anywhere,
    the Maven/Gradle caches' jars, or reading node_modules, the Go module cache or the Xcode SDK. The project's
    own build output (target/, build/) is not a library."""
    c = command.lower()
    if re.search(r"\bjavap\b|\bjar\s+-?t|\bcfr\b|\bprocyon\b|\bfernflower\b", c):
        return True
    # `.ja` at the very end: the progress summary cuts commands at 140 characters.
    if re.search(r"\bfind\b[^|;&]*-i?name\s+\S*\.ja(?:r\b|\S?$)", c) or "localrepository" in c:
        return True
    # Reading installed packages or the SDK to learn an API (cleanup such as `rm -rf node_modules` is not reading).
    # (an inline script loading a package's internals counts too: seen 2026-10-27, an hour of `node -e
    # "require('.../node_modules/@zxing/library')"` probing a decoder in two Pantry tickets)
    reads = re.search(r"(?:^|[\s;&|(])(?:find|grep|rg|cat|head|tail|less|sed|awk|ls)\s|\bnode\s+-e\b|"
                      r"\bpython3?\s+-c\b|\brequire\(", c)
    internals = re.search(r"node_modules/|/pkg/mod/|gomodcache|\.swiftinterface|\.platform/developer/sdks/|"
                          r"\.build/checkouts/", c)
    if reads and internals and not re.search(r"node_modules/\.bin/", c):
        return True
    # Fetching a library's source from its repository (seen: an hour in Tomcat's SSLUtilBase.java, 2026-10-25).
    if re.search(r"raw\.githubusercontent\.com/|api\.github\.com/repos/|github\.com/[^\s/]+/[^\s/]+/blob/", c):
        return True
    in_cache = any(k in c for k in (".m2/", "cache/m2", "m2/repository", ".gradle/caches", "cache/gradle"))
    return in_cache and any(k in c for k in (".jar", ".ja\"", "unzip", "find ", "ls ", "javap"))
LIB_NUDGE = ("Stop reading library internals (jars, library source, node_modules, the Go module cache, the Xcode SDK). "
             "When a library rejects your config, first check what it supports in its reference docs. To learn a "
             "library or SDK API run `law api <name> [member]` in the project (Java, Go, TypeScript/JavaScript, "
             "Swift, Monkey C): it prints the real signatures, or the closest real names when the one you used "
             "does not exist. Use only what it shows, compile, and let the test output guide you. Check "
             ".law/PROGRESS.md for notes, then continue the current ticket.")
# Looking up API after API without writing anything (seen 2026-10-21: LC-3 ran 20+ law api lookups over 1h40m,
# each followed by 10-20 min of planning, and wrote no code). After LOOKUP_LIMIT lookups since the last saved file
# the agent is told to write the code with what it has.
LOOKUP_LIMIT = 6
LOOKUP_NUDGE = ("You have looked up enough APIs. Write the code for the current ticket now, using what the lookups "
                "showed. Then compile and run the tests, and look up only a symbol the compiler reports as wrong. "
                "Small steps: write one file, build, fix, continue.")
DOC_ROLES = {"writer", "researcher", "designer", "analyst", "architect"}


def made_progress(output: str, is_work: Callable[[str], bool] | None = None) -> bool:
    """Whether an agent run did work (by default: saved any file) before it was stopped."""
    return any((is_work or is_file_change)(line) for line in output.splitlines())


def is_file_change(line: str) -> bool:
    """Progress for the stall guard: a file written, or a page fetched (research agents read before they write)."""
    summary = describe_opencode_line(line)
    return summary is not None and summary.startswith(("✏️", "🌐"))


def is_code_change(line: str) -> bool:
    """Progress for a ticket session's stall guard: a project file written. Editing LAW's plan or notes is not
    (seen 2026-10-28: a P-37.1 session rewrote its to-do list in PLAN.md after a stall nudge and read on, an hour
    without code)."""
    return is_file_change(line) and not in_law_folder(describe_opencode_line(line) or "")


def in_law_folder(summary: str) -> bool:
    """The progress line names a file in the project's LAW folder (absolute path, or relative as OpenCode 2 writes)."""
    return re.search(rf"(?:^|[\s/]){re.escape(FOLDER)}/", summary) is not None


def session_id(output: str) -> str | None:
    """The OpenCode session id from `--format json` output (the last one seen)."""
    found = None
    for line in output.splitlines():
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = ev.get("sessionID") or (ev.get("part") or {}).get("sessionID") or found
    return found


def parse_opencode_events(output: str) -> str:
    """Extract the final assistant text from `opencode run --format json` output."""
    texts: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") or {}
        if ev.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
    return texts[-1].strip() if texts else output[-4000:].strip()


def agent_env(rt: Runtime, role: str, effort: str | None = None) -> dict[str, str]:
    """Role config plus XDG dirs so OpenCode keeps its sessions, cache and state under ~/LAW, not ~/.local.
    `effort` picks the role's config with that thinking level (a project's `thinking`), when it exists."""
    base = rt.paths.data / "opencode"
    cache = rt.paths.cache
    config = rt.paths.config / f"opencode-{role}.json"
    if effort and (rt.paths.config / f"opencode-{role}-{effort}.json").exists():
        config = rt.paths.config / f"opencode-{role}-{effort}.json"
    return {"OPENCODE_CONFIG": str(config),
            "XDG_DATA_HOME": str(base / "data"), "XDG_STATE_HOME": str(base / "state"),
            "XDG_CACHE_HOME": str(cache / "opencode"),
            # build-tool caches the agents fill (measured: ~/.m2 and ~/.npm grew during one Java/Angular build)
            "npm_config_cache": str(cache / "npm"), "MAVEN_OPTS": f"-Dmaven.repo.local={cache / 'm2'}",
            "GRADLE_USER_HOME": str(cache / "gradle"), "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv"),
            # LAW's one Colima VM; agent shells skip the login profile, so `colima start` made a second VM in
            # ~/.colima (2026-10-25, Pantry environment stage)
            "COLIMA_HOME": str(rt.paths.home / "colima"),
            # docker talks to that VM even when the shared "colima" context is gone (`colima stop` on another
            # instance removes it)
            "DOCKER_HOST": f"unix://{rt.paths.home / 'colima' / 'default' / 'docker.sock'}",
            # Testcontainers mounts the socket into its Ryuk container: the path inside the Colima VM, not the host's
            "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE": "/var/run/docker.sock"}


_lead_tasks: set[asyncio.Task[None]] = set()


def make_handlers(rt: Runtime) -> dict[str, Any]:
    async def search(ctx: Context) -> Outcome:
        hits = await searx(rt, ctx.job.payload["query"], int(ctx.job.payload.get("limit", 8)))
        return Done({"results": hits})

    async def model_generating() -> bool:
        """The model server is producing tokens right now (oMLX's active requests)."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"http://127.0.0.1:{rt.reg.ports.llm_internal}/api/status")
                return int(r.json().get("active_requests", 0)) > 0
        except (httpx.HTTPError, ValueError):
            return False

    async def _agent(ctx: Context, role: str, prompt: str, workdir: Path, *, writes: bool = True,
                     memory: ProjectMemory | None = None, timeout_s: float | None = None,
                     finished: Callable[[], bool] | None = None, ticket: str | None = None) -> str:
        """Run one OpenCode agent. A writing agent that stalls (no file change for STALL_MINUTES) is stopped and
        its session resumed with a nudge to build and fix. With project memory, the session is recorded in
        `.law/session.json`, and a job that restarts (e.g. after a worker restart) resumes that session."""
        rt.roles.get(role)  # validate
        model = f"law/{rt.model_id(role)}"
        stall_min = float(ctx.job.payload.get("stall_min", STALL_MINUTES))
        base = ["opencode", "run", "--agent", role, "-m", model, "--format", "json", "--dir", str(workdir)]
        resumed = memory.session_for(ctx.job.id) if memory else None
        if resumed:
            await ctx.progress(f"{role} agent resuming its interrupted session in {workdir}")
            section = ctx.job.payload.get("section")
            again = (f"Your previous session was interrupted. Finish section \"{section['heading']}\" in "
                     f"{section['file']} (append it if it is missing; do not repeat it if it is there), then stop. "
                     f"LAW tracks the sections: do not edit {FOLDER}/PLAN.md or {FOLDER}/PROGRESS.md.") if section \
                else (memory.resume_prompt() if memory else "")
            cmd = [*base, "--session", resumed, again]
        else:
            await ctx.progress(f"{role} agent working in {workdir}")
            cmd = [*base, prompt]

        started = time.time()
        # one deadline for the whole session: a nudge resumes it in a new run, which must not restart the clock
        deadline = time.monotonic() + (timeout_s or float(ctx.job.payload.get("timeout_s", 4 * 3600)))
        edits: list[str] = []
        recent: list[str] = []  # LAW's summaries of the agent's actions: what the lead step reads

        def describe(line: str) -> str | None:
            if memory is not None and line.startswith('{"'):
                sid = session_id(line)
                if sid:
                    memory.save_session(ctx.job.id, role, sid)
            summary = describe_opencode_line(line)
            if summary and summary.startswith("✏️"):
                edits.append(summary.split(" ", 2)[-1])
            if summary:
                recent.append(f"{time.strftime('%H:%M')} {summary}")
                del recent[:-200]
            return summary

        digs = lookups = blind_edits = reads = probes = 0
        told_law = told_probe = told_shell_write = told_wrap = False
        opened: dict[str, int] = {}  # file -> times read since it was last edited

        def steer(summary: str) -> str | None:
            nonlocal digs, lookups, blind_edits, reads, probes, told_law, told_probe, told_shell_write, told_wrap
            if finished is not None and finished():
                return TICKET_DONE
            # a handover writer only documents: a change request waits for an agent that can build it (seen 2026-10-30:
            # the owner's icon and photo requests became "known limitations" in the handover document)
            holds_notes = ctx.job.payload.get("spec_ref") == "project.handover"
            note = memory.take_notes() if memory is not None and not holds_notes else None
            if note:
                return OWNER_NOTE.format(note=note)
            if role == "qa" and reads_product_code(summary, workdir):
                return QA_CODE_NUDGE
            if not told_law and reads_law_internals(summary, rt.paths.repo, rt.paths.home):
                told_law = True
                from law.handlers.project import law_services

                return LAW_INTERNALS_NUDGE + law_services()
            if role in DOC_ROLES:
                if not told_wrap and not ctx.job.payload.get("project") and time.time() - started > WRAP_MINUTES * 60:
                    told_wrap = True
                    return WRAP_NUDGE.format(minutes=int((time.time() - started) // 60))
                if DOC_PROBE.search(summary):
                    probes += 1
                    if probes % DOC_PROBE_LIMIT == 0:  # again every few: one reminder was not enough on 2026-11-23
                        return DOC_PROBE_NUDGE
                return None
            if role != "coder":
                return None
            if summary.startswith("▶") and RUNS_CHECKS.search(summary):
                blind_edits = 0
            if summary.startswith("▶") and not told_shell_write and SHELL_WRITE.search(summary):
                told_shell_write = True
                return SHELL_WRITE_NUDGE
            # LAW's plan and notes are reread by design (seen 2026-10-28: the split nudge sent a P-34 session to
            # PLAN.md, and its third open of the plan cut the split off with "write the change")
            if summary.startswith("📖") and not in_law_folder(summary):
                name = summary.split(" ", 2)[-1].strip()
                opened[name] = opened.get(name, 0) + 1
                if opened[name] >= REREAD_LIMIT and role == "coder":
                    times, opened[name] = opened[name], 0
                    return REREAD_NUDGE.format(name=name.rsplit("/", 1)[-1], times=times)
            if summary.startswith(("📖", "🔎")):
                reads += 1
                if reads >= READ_LIMIT:
                    reads = 0
                    return READ_NUDGE
            if summary.startswith("✏️ writing") and PROBE_FILE.search(summary.split(" ", 2)[-1].strip()):
                probes += 1
                if probes >= PROBE_LIMIT and not told_probe:
                    told_probe = True
                    return PROBE_NUDGE
            if summary.startswith("✏️"):
                reads = 0
                opened.pop(summary.split(" ", 2)[-1].strip(), None)
                blind_edits += 1
                if blind_edits >= BLIND_EDIT_LIMIT:
                    blind_edits = 0
                    return RUN_CHECKS_NUDGE
                digs = lookups = 0
                return None
            if summary.startswith("▶") and is_library_dig(summary):
                digs += 1
                if digs >= LIB_DIG_LIMIT:
                    digs = 0
                    return LIB_NUDGE
            if summary.startswith("▶"):
                lookups += summary.count("law api ")
                if lookups >= LOOKUP_LIMIT:
                    lookups = 0
                    return LOOKUP_NUDGE
            return None

        # Interventions in a row without progress: an agent that saved files since the last nudge earned a fresh
        # budget (one stage runs a whole milestone for hours; seen 2026-10-21: two tickets committed between
        # nudges, then the fourth nudge failed the pipeline).
        strikes = qa_reminders = locked = 0
        code_only = finished is not None and role not in DOC_ROLES  # a coder's ticket: only code counts as work
        work = is_code_change if code_only else is_file_change
        while True:
            try:
                env = {**agent_env(rt, role, ctx.job.payload.get("effort")), **project_env(workdir)}
                out = await run_cmd(ctx, cmd, cwd=workdir, env=env, describe=describe,
                                    timeout_s=max(1.0, deadline - time.monotonic()),
                                    stall_s=stall_min * 60 if writes and stall_min > 0 else None,
                                    is_work=work, alive=model_generating,
                                    changed_since=lambda since: changed_since(workdir, since, code_only=code_only),
                                    steer=steer)
                answer_text = parse_opencode_events(out)
                if memory is not None and writes and memory.ensure_progress(started, role, answer_text, edits):
                    await ctx.progress("📝 recorded this step in .law/PROGRESS.md (the agent had not)")
                return answer_text
            except Fatal as exc:
                # OpenCode's own SQLite store can still be locked by the previous session closing (seen 2026-10-29:
                # stage 13 started one second after stage 12's agent exited, OpenCode quit with "database is
                # locked" and the whole Pantry pipeline failed). That is transient: wait and start again.
                if DB_LOCKED not in str(exc) or locked >= len(DB_LOCKED_WAITS_S):
                    raise
                await ctx.progress(f"⏳ OpenCode's database is busy: retrying in {DB_LOCKED_WAITS_S[locked]} s")
                await asyncio.sleep(DB_LOCKED_WAITS_S[locked])
                locked += 1
                continue
            except Steered as exc:
                sid = session_id(exc.output)
                if exc.message == TICKET_DONE:  # its ticket is ticked and committed: LAW checks it next
                    await ctx.progress("✅ ticket done: ending the session so LAW checks it")
                    return parse_opencode_events(exc.output)
                if exc.message == QA_CODE_NUDGE and sid is not None and qa_reminders < 3:
                    qa_reminders += 1
                    await ctx.progress("⚠️ QA started reading the source: back to testing the product")
                    cmd = [*base, "--session", sid, exc.message]
                    continue
                if exc.message == SHELL_WRITE_NUDGE and sid is not None:  # a reminder of the house standard
                    await ctx.progress("⚠️ edited a file with a shell command: reminding the agent to use the edit tool")
                    cmd = [*base, "--session", sid, exc.message]
                    continue
                if exc.message == PROBE_NUDGE and sid is not None:  # it is working, just in a loop: not a strike
                    await ctx.progress("⚠️ debugging with probe files: telling the agent to time-box and fall back")
                    cmd = [*base, "--session", sid, exc.message]
                    continue
                if exc.message.startswith(LAW_INTERNALS_NUDGE) and sid is not None:  # guidance, not a strike
                    await ctx.progress("⚠️ reading LAW's own source or state: pointing the agent to LAW's interfaces")
                    cmd = [*base, "--session", sid, exc.message]
                    continue
                if exc.message.startswith(OWNER_NOTE_HEAD) and sid is not None:  # the owner's word, not a strike
                    await ctx.progress("📨 passing the owner's note to the agent")
                    cmd = [*base, "--session", sid, exc.message]
                    continue
                strikes = 1 if made_progress(exc.output, work) else strikes + 1
                if sid is None or strikes > STALL_NUDGES:
                    raise Fatal(f"{role} agent kept researching without writing code") from exc
                why = {LOOKUP_NUDGE: "looked up enough APIs: telling the agent to write the code",
                       RUN_CHECKS_NUDGE: "many edits without a build or test run: telling the agent to run them",
                       READ_NUDGE: "read enough: telling the agent to write the change"}.get(
                    exc.message, "reading library internals: redirecting the agent to compile and test instead")
                if exc.message.startswith("You have opened"):
                    why = "reading the same file again: telling the agent to write the change"
                await ctx.progress(f"⚠️ {why}")
                cmd = [*base, "--session", sid, exc.message]
                continue
            except Stalled as exc:
                sid = session_id(exc.output)
                strikes = 1 if made_progress(exc.output, work) else strikes + 1
                if sid is None or strikes > STALL_NUDGES:
                    raise Fatal(f"{role} agent stalled: {exc}") from exc
                nudge = NUDGE_DOCS if role in DOC_ROLES else NUDGE
                what = "save the next section" if role in DOC_ROLES else "build and fix"
                if finished is not None and role not in DOC_ROLES and strikes >= 2:
                    nudge, what = SPLIT_NUDGE, "split the ticket into smaller steps"
                message = nudge.format(minutes=f"{stall_min:.0f}")
                left = uncommitted(workdir) if finished is not None and role not in DOC_ROLES else []
                if left and (await _gate_problem(ctx, workdir, after_ticket=False)).startswith(UNCOMMITTED_HEAD):
                    message, what = COMMIT_NUDGE.format(files=", ".join(left[:10])), "tick and commit"
                await ctx.progress(f"⚠️ nothing saved for {stall_min:.0f} min: nudging the agent to {what}")
                if ticket and role in ("coder", "writer") and strikes >= 2:
                    _draft_lead_note(ctx, role, workdir, ticket, list(recent))
                cmd = [*base, "--session", sid, message]

    def _draft_lead_note(ctx: Context, role: str, workdir: Path, ticket: str, activity: list[str]) -> None:
        """Ask the lead (the role's model) for a note in the background; a note becomes a draft the owner approves
        on Telegram before the agent sees it (ADR-0026). One undecided draft per job at a time."""
        leads = lead.Leads(rt.paths)
        if leads.pending_for(ctx.job.id) is not None:
            return

        async def draft() -> None:
            try:
                answer = await rt.llm.chat(rt.model_id(role), [
                    {"role": "system", "content": lead.PROMPT},
                    {"role": "user", "content": lead.context_message(ticket, activity, workdir)}],
                    temperature=1.0, max_tokens=16000)
            except Exception as exc:  # the lead is an extra: its failure never stops the agent
                await ctx.progress(f"🧭 lead note not drafted: {type(exc).__name__}")
                return
            stuck, diagnosis, note = lead.parse(answer)
            if not stuck:
                await ctx.progress("🧭 the lead looked: not stuck, no note")
                return
            project = str(ctx.job.payload.get("project") or workdir.name)
            leads.add(project=project, folder=workdir, ticket=ticket, job=ctx.job.id, diagnosis=diagnosis, note=note)
            await ctx.progress("🧭 the lead drafted a note: waiting for the owner's approval on Telegram")

        task = asyncio.create_task(draft())
        _lead_tasks.add(task)  # keep a reference so the task isn't garbage-collected
        task.add_done_callback(_lead_tasks.discard)

    def _scoped(p: dict[str, Any], memory: ProjectMemory | None = None, writes: bool = True) -> str:
        """The agent's prompt: the brief (or the raw request), the folder to work in, and how to keep `.law/`."""
        carry = p.get("_carry") or {}
        parts = []
        if _dir(p):  # a named or chosen folder: build there, not in a new one
            parts.append(f"Work in this directory and create the project files directly in it: {_workdir(p)}\n"
                         f"Do not create a different project folder.")
        if memory is not None and p.get("section"):
            # one section of a document LAW assembles: LAW tracks the sections and records progress
            parts.append(f"LAW tracks this document's sections and records progress. Do not edit {FOLDER}/PLAN.md "
                         f"or {FOLDER}/PROGRESS.md; just write the section.")
        elif memory is not None:
            parts.append(memory.instructions(ticket=bool(p.get("milestone"))))  # a build stage runs ticket sessions
            if p.get("project"):  # project sessions learn from earlier projects and teach later ones
                parts.append(LESSON_RULE)
                if learned := Lessons(rt.paths).prompt(str(p["role"])):
                    parts.append(learned)
        elif writes:
            parts.append(NEW_PROJECT_MEMORY)
        parts.append(str(carry.get("brief") or p["prompt"]))
        spec = specs.resolve(p)  # fixed instructions from the pipeline (e.g. /proposal's structure), current version
        if spec:
            parts.append(spec)
        return "\n\n".join(parts)

    def _memory(ctx: Context, p: dict[str, Any]) -> ProjectMemory | None:
        """Project memory lives in the folder the user named; a new project creates its own `.law/`."""
        if not _dir(p):
            return None
        memory = ProjectMemory(_workdir(p))
        memory.prepare(ctx.job.parent or ctx.job.id, str(p["prompt"]), (p.get("_carry") or {}).get("brief"))
        return memory

    def _dir(p: dict[str, Any]) -> str | None:
        """The folder the user named, or the one the translator chose for a new project."""
        return p.get("dir") or (p.get("_carry") or {}).get("dir")

    def _workdir(p: dict[str, Any]) -> Path:
        named = _dir(p)
        workdir = Path(named or rt.paths.projects).expanduser()
        if named and not workdir.exists() and workdir.parent.resolve() == rt.paths.projects.resolve():
            workdir.mkdir()  # `/code ~/SourceCode/new-app ...` starts a new project in that folder
        if not workdir.is_dir():
            raise Fatal(f"directory does not exist: {workdir}")
        return workdir

    async def task(ctx: Context) -> Outcome:
        """Run a role as an OpenCode agent. Code-writing roles get an independent review and a fix pass."""
        p = ctx.job.payload
        role = p.get("role", "coder")
        workdir = _workdir(p)
        translate = p.get("translate", role in REVIEWED_ROLES)
        review = p.get("review", role in REVIEWED_ROLES)
        if (translate or review) and ctx.job.parent is None:
            base = {"prompt": p["prompt"], "role": role, **({"dir": str(workdir)} if p.get("dir") else {})}
            stages = [{"kind": "task_translate", "payload": base}] if translate else []
            stages.append({"kind": "task_work", "payload": base})
            if review:
                stages += [{"kind": "task_review", "payload": base}, {"kind": "task_fix", "payload": base}]
            return Stages(stages)
        writes = role in REVIEWED_ROLES
        memory = _memory(ctx, p) if writes else None
        text = await _agent(ctx, role, _scoped(p, memory, writes), workdir, writes=writes, memory=memory)
        return Done({"answer": text, "dir": str(workdir)})

    async def task_translate(ctx: Context) -> Outcome:
        """The translator turns the request into a precise brief, aware of what the project folder already holds."""
        p = ctx.job.payload
        workdir = _workdir(p)
        named = bool(p.get("dir"))
        choose = not named and p["role"] == "coder"
        root = rt.paths.projects
        projects = sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")) \
            if root.is_dir() else []
        prompt = TRANSLATE.format(
            role=p["role"], request=p["prompt"],
            folder=workdir if named else "a new folder (see below)",
            listing=project_listing(workdir) if named else "(new project)",
            memory=(ProjectMemory(workdir).context() if named else "") or "(none)",
            fixed=FIXED.format(spec=specs.resolve(p)) if specs.resolve(p) else "",
            choose=CHOOSE_FOLDER.format(root=root, projects=", ".join(projects) or "(none)") if choose else "")
        await ctx.progress("📝 translating the request into a brief")
        brief = (await answer(rt, "translator", [{"role": "user", "content": prompt}], ctx)).strip()
        if not brief:
            raise Fatal("the translator returned an empty brief")
        result: dict[str, Any] = {"brief": brief}
        if choose:
            folder = project_folder(brief, root)
            if folder is None:
                raise Fatal("the translator did not name a valid project folder under " + str(root))
            folder.mkdir(parents=True, exist_ok=True)
            result["dir"] = str(folder)
            await ctx.progress(f"📁 project folder: {folder}")
        await ctx.progress(f"📝 brief ready ({len(brief.split())} words)")
        return Done(result)

    async def task_work(ctx: Context) -> Outcome:
        p = ctx.job.payload
        if p.get("milestone") and plan_complete(_workdir(p), int(p.get("version", 1))):
            return Done({"dir": str(_workdir(p))})  # every milestone of this version is ticked
        section = p.get("section")
        if section and section.get("skip_if_written"):
            from law.handlers.proposal import section_written

            doc = _workdir(p) / section["file"]
            if doc.is_file() and section_written(doc.read_text(), section["heading"]):
                return Done({"dir": str(_workdir(p))})  # this section is already in the document
        memory = _memory(ctx, p)
        if p.get("milestone"):
            return await _build_tickets(ctx, p, memory)
        plan_before = memory.plan_text() if memory is not None else ""
        text = await _agent(ctx, p["role"], _scoped(p, memory), _workdir(p), memory=memory)
        if memory is not None and memory.keep_build_sections(plan_before):
            await ctx.progress("LAW put back the build tickets this session removed from .law/PLAN.md")
        record = _workdir(p) / ".law" / "project.json"
        if record.is_file() and not p.get("project"):  # a project an agent scaffolded by hand still reaches the board
            with contextlib.suppress(OSError, ValueError):
                Projects(rt.paths).adopt(_workdir(p))
        return Done({"answer": text, "dir": str(_workdir(p))})

    async def _gate_problem(ctx: Context, folder: Path, *, after_ticket: bool = True, commit_as: str | None = None,
                            reply: str = "", before: str = "") -> str:
        """What is wrong after a ticket, from LAW's own checks (not the agent's word): `.law/check.sh` fails, is
        missing, or product files were left uncommitted. Empty when the ticket is really done. With `commit_as`
        (the session's ticket line, still open), a passing gate makes LAW commit, tick and record the ticket."""
        script = folder / CHECK_SCRIPT
        if not script.is_file():
            return f"{CHECK_SCRIPT} does not exist. Create it now (see the build rules above)."
        await ctx.progress(f"🔍 LAW runs {CHECK_SCRIPT} (the project's whole gate)")
        env = {**agent_env(rt, "coder"), **project_env(folder)}
        try:
            await run_cmd(ctx, ["bash", CHECK_SCRIPT], cwd=folder, env=env, timeout_s=CHECK_TIMEOUT_S,
                          progress_every=10**9)  # the build log is not progress
        except Fatal as exc:
            await ctx.progress(f"❌ {CHECK_SCRIPT} failed: " + ("the next session fixes it first" if after_ticket
                                                              else "the agent is still working on it"))
            return f"{CHECK_SCRIPT} failed:\n{str(exc)[-CHECK_TAIL:]}"
        except TimeoutError:
            return f"{CHECK_SCRIPT} did not finish within {CHECK_TIMEOUT_S // 60} minutes."
        tid = TICKET_ID.match(commit_as or "")
        plan = folder / FOLDER / "PLAN.md"
        if tid and plan.is_file() and re.search(rf"(?m)^\s*- \[ \]\s*{re.escape(tid.group(0))}\b", plan.read_text()):
            try:
                unfinished = finish_ticket(folder, str(commit_as), reply, before)
            except RuntimeError as exc:
                unfinished = f"LAW could not commit the ticket: {exc}"
            if unfinished:
                await ctx.progress(f"🔸 {unfinished.split('. ')[0]}")
                return unfinished
            await ctx.progress(f"☑️ LAW committed and ticked {tid.group(0)}")
        status = await run_cmd(ctx, ["git", "status", "--porcelain"], cwd=folder, timeout_s=60)
        left = [ln[3:] for ln in status.splitlines() if ln[3:] and not ln[3:].startswith(f"{FOLDER}/")]
        if left:
            return UNCOMMITTED_HEAD + ", ".join(left[:20]) + \
                ". Commit them with the ticket they belong to, or remove them."
        await ctx.progress(f"✅ {CHECK_SCRIPT} passed and the work is committed")
        return ""

    async def _build_tickets(ctx: Context, p: dict[str, Any], memory: ProjectMemory | None) -> Outcome:
        """One milestone, one fresh agent session per ticket (ADR-0019). LAW names each session's ticket, then
        checks it itself; what fails goes to the next session to fix first. The stage ends when the milestone's
        tickets are ticked and the gate passes, or after IDLE_SESSIONS sessions without progress."""
        folder, version = _workdir(p), int(p.get("version", 1))
        start = next_ticket(folder, version)
        problem, idle, text = "", 0, ""
        began = time.monotonic()
        for _ in range(TICKETS_PER_STAGE):
            used = time.monotonic() - began
            left = JOB_TIMEOUT_S - used - CHECK_TIMEOUT_S - STAGE_MARGIN_S
            if used >= STAGE_BUDGET_S or left < MIN_SESSION_S:
                await ctx.progress("⏱ this stage's time is used: the next stage continues the milestone")
                break
            for tid in tick_split_parents(folder):
                await ctx.progress(f"☑️ {tid}: all its sub-tickets are ticked, so LAW ticked it too")
            ticket = next_ticket(folder, version)
            if ticket is None and build_section(folder, version) is not None and not problem:
                break
            if start is not None and ticket is not None and ticket[0] != start[0] and not problem:
                break  # this milestone is done; the next stage takes the next one
            task = TICKET_SESSION.format(ticket=ticket[1], milestone=ticket[0] or "-") if ticket else \
                ALL_TICKED if build_section(folder, version) is not None else FIRST_TICKET
            if ticket:
                task += "\n\n" + TARGETED.format(map=project_map(folder))
                if context := ticket_context(folder, version, ticket[0]):
                    task += "\n\n" + context
            prompt = f"{_scoped(p, memory)}\n\n{GATE_FAILED.format(problem=problem) if problem else ''}{task}"
            def ticket_done(session_ticket: list[tuple[str, str] | None] = [ticket]) -> bool:  # noqa: B006
                current, now = session_ticket[0], next_ticket(folder, version)
                if current is None or now == current:
                    return False
                if now is not None and is_split_of(now[1], current[1]):
                    session_ticket[0] = now  # split: the session goes on with the first part it just planned
                    return False
                return not uncommitted(folder)

            before = head(folder)
            try:
                text = await _agent(ctx, p["role"], prompt, folder, memory=memory,
                                    timeout_s=min(left, float(p.get("timeout_s", 4 * 3600))),
                                    finished=ticket_done, ticket=ticket[1] if ticket else None)
            except TimeoutError:
                if memory is not None:
                    memory.end_session(ctx.job.id)
                await ctx.progress("⏱ the session used this stage's time: the next stage continues the ticket")
                break
            if memory is not None:
                memory.end_session(ctx.job.id)  # the next ticket starts a fresh session, not this one
            had_problem = bool(problem)
            problem = await _gate_problem(ctx, folder, commit_as=ticket[1] if ticket else None,
                                          reply=text, before=before)
            if not problem:  # proven in this project: other projects of this role get it from now on
                for title in Lessons(rt.paths).harvest(folder, str(p["role"])):
                    await ctx.progress(f"📘 lesson kept for later projects: {title}")
            moved = next_ticket(folder, version) != ticket
            idle = 0 if moved or (had_problem and not problem) else idle + 1
            if start is None:
                start = next_ticket(folder, version)
            if idle >= IDLE_SESSIONS:
                return Done({"answer": text, "dir": str(folder)})
        # Open tickets left: ask the worker for one more build stage if the pipeline has none left (ADR-0021).
        return Done({"answer": text, "dir": str(folder), "_again": next_ticket(folder, version) is not None})

    async def _qa_round(ctx: Context, p: dict[str, Any]) -> dict[str, Any]:
        """Black-box QA of a project: a fresh session of the QA role (another model than the coder) tests the
        running product against the requirements and keeps .law/BUGS.md; the unresolved bugs go to the fix stage."""
        folder = _workdir(p)
        a = p.get("spec_args") or {}
        spec_file = a.get("spec_file") or f"{a.get('out', 'docs/proposal')}/proposal.md"
        before = {b.id for b in bugs.load(folder)}
        if a.get("kind") == "document":
            return await _editor_round(ctx, p, folder, a, spec_file, before)
        await ctx.progress("🧪 QA: testing the product as a user (black box)")
        task = QA_TASK.format(name=a.get("name", folder.name), version=p.get("version", 1), folder=folder,
                              spec_file=spec_file)
        reply = await _agent(ctx, "qa", task, folder, writes=False)
        report = json_report(reply)
        if not report.get("tested"):  # seen 2026-10-29: a session stopped mid-sentence and counted as "no bugs", 5/5
            await ctx.progress("🧪 QA ended without a report: testing again")
            reply = await _agent(ctx, "qa", task + QA_NO_REPORT, folder, writes=False)
            report = json_report(reply)
        missing = [] if report.get("tested") else ["no test results"]
        if not qa_pack_kept(folder):
            missing.append("no regression checks in qa/")
        docs = design_docs(folder)
        if docs:  # QA's model cannot see images, so the designer (a vision model) checks the look
            await ctx.progress("🎨 design review: the screens against the design")
            shown = await _agent(ctx, "designer", DESIGN_TASK.format(
                name=a.get("name", folder.name), version=p.get("version", 1), folder=folder,
                design=", ".join(docs)), folder, writes=False)
            if not json_report(shown).get("screens"):
                missing.append("no design review of the screens")
        open_bugs = bugs.unresolved(folder)
        blocking = [b for b in open_bugs if b.severity in bugs.BLOCKING]
        summary = str(report.get("summary") or reply.strip()[-300:])
        await ctx.progress(f"🧪 QA: {len(blocking)} blocking, {len(open_bugs) - len(blocking)} minor. {summary}"[:300])
        new_blockers = [b for b in blocking if b.id not in before and b.severity == "blocker"]
        if new_blockers:
            await _alert_owner(a.get("name", folder.name), new_blockers)
        evidence = [NO_EVIDENCE.format(what=", ".join(missing))] if missing else []
        return {"open": [*evidence, *bugs.as_issues(blocking)],
                "minor": bugs.as_issues([b for b in open_bugs if b not in blocking]),
                "tested": [str(t) for t in report.get("tested", [])][:40], "summary": summary}

    async def _editor_round(ctx: Context, p: dict[str, Any], folder: Path, a: dict[str, Any], spec_file: str,
                            before: set[str]) -> dict[str, Any]:
        """QA for a document: the editor (judge model, another family than the writer) fact-checks and checks the
        build; the designer (a vision model) checks every rendered page for cut or unreadable figures."""
        fill = {"name": a.get("name", folder.name), "version": p.get("version", 1), "folder": folder,
                "spec_file": spec_file}
        await ctx.progress("🧐 editor: fact-checking every chapter against its sources")
        reply = await _agent(ctx, "editor", EDITOR_TASK.format(**fill), folder, writes=False)
        report = json_report(reply)
        if not report.get("tested"):
            reply = await _agent(ctx, "editor", EDITOR_TASK.format(**fill) + QA_NO_REPORT, folder, writes=False)
            report = json_report(reply)
        await ctx.progress("🖼 page check: every page for cut or unreadable figures")
        shown = await _agent(ctx, "designer", PAGES_TASK.format(**fill), folder, writes=False)
        missing = ([] if report.get("tested") else ["no fact-check results"]) + \
            ([] if json_report(shown).get("screens") else ["no page check"])
        open_bugs = bugs.unresolved(folder)
        blocking = [b for b in open_bugs if b.severity in bugs.BLOCKING]
        summary = str(report.get("summary") or reply.strip()[-300:])
        minor = len(open_bugs) - len(blocking)
        await ctx.progress(f"🧐 editor: {len(blocking)} blocking, {minor} minor. {summary}"[:300])
        new_blockers = [b for b in blocking if b.id not in before and b.severity == "blocker"]
        if new_blockers:
            await _alert_owner(a.get("name", folder.name), new_blockers)
        evidence = [NO_EVIDENCE.format(what=", ".join(missing))] if missing else []
        return {"open": [*evidence, *bugs.as_issues(blocking)],
                "minor": bugs.as_issues([b for b in open_bugs if b not in blocking]),
                "tested": [str(t) for t in report.get("tested", [])][:40], "summary": summary}

    async def _alert_owner(name: str, found: list[bugs.Bug]) -> None:
        """A new blocker reaches the owner at once, with its steps and screenshot, not only at the next gate."""
        for bug in found:
            text = f"🚨 QA found a blocker in {name}: {bug.id} {bug.title}\n\n{bug.body}"[:1000]
            with contextlib.suppress(deliver.DeliveryError):  # not set up: it is still on the board and at the gate
                await asyncio.to_thread(deliver.to_telegram, rt.paths.env_file, bug.screenshots[:1], text)

    async def task_review(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = ctx.job.payload["_carry"]
        if carry.get("passed"):  # an earlier review round already passed
            return Done({})
        qa = await _qa_round(ctx, p) if p.get("project") and p.get("rubric_ref") == "project.review" else None
        if qa is not None and p.get("qa_only"):  # /qa: the product only, no code review
            only: dict[str, Any] = {"score": PASS_SCORE - 1 if qa["open"] else 5,
                                    "issues": [*qa["open"], *qa["minor"]][:16], "summary": f"QA: {qa['summary']}",
                                    "qa": qa}
            return Done({"review": only})
        report = carry.get("answer", "")
        brief = f"\n\nThe author worked from this brief:\n{carry['brief'][:6000]}" if carry.get("brief") else ""
        rubric = specs.resolve(p, "rubric") or CODE_RUBRIC
        prompt = (f"{rubric}\n\nThe REQUEST was:\n{p['prompt']}{brief}\n\nThe author reported:\n{report[:4000]}\n\n"
                  "Inspect the files the author created or changed (the project may be in a new folder under "
                  "~/SourceCode named after it). Verify claims against the code; do not trust the report.")
        reply = await _agent(ctx, "reviewer", prompt, _workdir(p), writes=False)
        verdict = parse_verdict(reply)
        if verdict.get("unscored") and len(reply.strip()) < EMPTY_REVIEW_CHARS:
            # no findings to restate (seen: a compacted session ended with one line, then a restated 5/5): review again
            await ctx.progress("the review ended without findings: reviewing again")
            reply = await _agent(ctx, "reviewer", prompt + "\n\nRead each file once, note problems as you go, and end "
                                 "with the JSON verdict.", _workdir(p), writes=False)
            verdict = parse_verdict(reply)
        if verdict.get("unscored"):  # ask the same model to state its verdict in the required format
            await ctx.progress("the reviewer gave no score: asking it to state its verdict")
            again = await answer(rt, "reviewer", [{"role": "user", "content": (
                f"Here is your review:\n\n{reply[-6000:]}\n\nState its verdict as JSON only: "
                '{"score": <1-5>, "issues": ["<file or section: problem and fix>"], "summary": "<one sentence>"}')}],
                ctx)
            verdict = parse_verdict(again)
        if qa is not None:  # a round passes only when QA has no unresolved bug and the code review passes
            score = int(verdict.get("score") or 0)
            verdict = {**verdict, "score": min(score, PASS_SCORE - 1) if qa["open"] else score,
                       "issues": [*qa["open"], *verdict.get("issues", [])][:16],
                       "summary": f"QA: {qa['summary']} Code review: {verdict.get('summary', '')}".strip(),
                       "qa": qa}
        if carry.get("diagram_errors"):  # checked by drawing them: the reviewer cannot see this
            verdict = {**verdict, "score": min(int(verdict.get("score") or 0), PASS_SCORE - 1),
                       "issues": [*carry["diagram_errors"], *verdict.get("issues", [])][:12]}
        return Done({"review": verdict})

    async def task_fix(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = ctx.job.payload["_carry"]
        verdict = carry.get("review") or {"score": 1, "issues": list(p.get("issues") or []), "summary": ""}
        if carry.get("passed"):
            return Done({})
        if verdict["score"] >= PASS_SCORE:
            return Done({"answer": carry.get("answer", ""), "review": verdict, "fixed": False, "passed": True,
                         "dir": carry.get("dir")})
        issues = "\n".join(f"- {i}" for i in verdict["issues"]) or "- raise the quality to client-ready"
        prompt = (f"An independent reviewer scored your work {verdict['score']}/5 for this request:\n{p['prompt']}\n\n"
                  f"Fix every issue below, then rerun the project's checks until they pass and report the output:\n"
                  f"{issues}")
        if any("BUG-" in i for i in verdict["issues"]):
            prompt += ("\n\nBUG-n items are in .law/BUGS.md (steps, expected, actual). Reproduce each, fix it, add a "
                       "test that would have caught it, then change its status word in the heading to fixed and add a "
                       "line \"Fix: <commit> <what changed>\". Never set verified or reopened: QA retests. Do not "
                       "edit qa/: those are QA's regression checks.")
        spec = specs.resolve(p)
        if spec:
            prompt += f"\n\nThe work must still meet these instructions:\n{spec}"
        memory = _memory(ctx, p)
        if memory is not None:
            prompt = f"{memory.instructions()}\n\n{prompt}"
        text = await _agent(ctx, p["role"], prompt, _workdir(p), memory=memory)
        return Done({"answer": text, "review": verdict, "fixed": True, "passed": False, "dir": carry.get("dir")})

    return {"search": search, "task": task, "task_translate": task_translate, "task_work": task_work,
            "task_review": task_review, "task_fix": task_fix}
