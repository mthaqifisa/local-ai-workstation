"""A project's bug list in `<project>/.law/BUGS.md`: what QA and the owner find in the product, and where each bug is.

One file is the single source for the QA agent (files and retests bugs), the coder (fixes them), Telegram
(`/bugfix` reports one) and the Console board (a ticket per bug). Each bug is a level-2 heading followed by its
report:

    ## BUG-3 · open · owner · 2026-10-26 07:10
    Scanning a known barcode twice shows no "already at home" alert
    Severity: major
    Steps: ... Expected: ... Actual: ...

Severity (a `Severity:` line; `major` when missing): `blocker` (a core requirement fails), `major` (a requirement
works wrongly), `minor` (cosmetic or an edge with a workaround). Only blocker and major bugs hold a round back.

Statuses: `open` (reported), `fixed` (the coder says so; QA has not retested), `verified` (QA retested: gone),
`reopened` (QA retested: still there). Only QA moves a bug to `verified` or `reopened`.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Status = Literal["open", "fixed", "verified", "reopened"]
STATUSES: tuple[Status, ...] = ("open", "fixed", "verified", "reopened")
UNRESOLVED: frozenset[str] = frozenset({"open", "fixed", "reopened"})  # a fixed bug counts until QA verifies it
SEVERITIES = ("blocker", "major", "minor")
BLOCKING: frozenset[str] = frozenset({"blocker", "major"})
SEVERITY = re.compile(r"^\s*Severity:\s*(blocker|major|minor)\b", re.IGNORECASE | re.MULTILINE)
SHOT = re.compile(r"(\S+\.(?:png|jpe?g))\b")
HEADING = re.compile(r"^##\s+(BUG-\d+)\s*·\s*([a-z]+)\s*·\s*([^·]+?)\s*·\s*(.+?)\s*$")
FILE = "BUGS.md"
HEADER = ("# Bugs\n\nFound by QA (testing the product, not the code) and reported by the owner. "
          "Statuses: open, fixed (awaiting QA retest), verified, reopened. Only QA sets verified or reopened.\n")


class Bug(BaseModel):
    id: str
    status: Status
    reporter: str
    when: str
    title: str
    body: str = ""

    @property
    def severity(self) -> str:
        m = SEVERITY.search(self.body)
        return m.group(1).lower() if m else "major"

    @property
    def screenshots(self) -> list[Path]:
        """Evidence images the report names that exist on this Mac."""
        found = [Path(m).expanduser() for m in SHOT.findall(self.body)]
        return [f for f in found if f.is_file()]


def path(project: Path) -> Path:
    return project / ".law" / FILE


def parse(text: str) -> list[Bug]:
    bugs: list[Bug] = []
    current: dict[str, str] | None = None
    lines: list[str] = []

    def close() -> None:
        if current is not None:
            title, *rest = [*(ln for ln in lines if ln.strip()), ""]
            status = current["status"] if current["status"] in STATUSES else "open"
            bugs.append(Bug(id=current["id"], status=status, reporter=current["reporter"],
                            when=current["when"], title=title.strip(), body="\n".join(rest).strip()))

    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            close()
            current = {"id": m.group(1), "status": m.group(2), "reporter": m.group(3), "when": m.group(4)}
            lines = []
        elif current is not None:
            lines.append(line)
    close()
    return bugs


def load(project: Path) -> list[Bug]:
    try:
        return parse(path(project).read_text())
    except OSError:
        return []


def unresolved(project: Path) -> list[Bug]:
    return [b for b in load(project) if b.status in UNRESOLVED]


def blocking(project: Path) -> list[Bug]:
    """Unresolved blocker and major bugs: what keeps a round from passing."""
    return [b for b in unresolved(project) if b.severity in BLOCKING]


def add(project: Path, title: str, details: str = "", *, reporter: str = "owner", severity: str = "major") -> Bug:
    """Append a new open bug and return it (ids are never reused)."""
    if not title.strip():
        raise ValueError("describe the bug")
    file = path(project)
    file.parent.mkdir(parents=True, exist_ok=True)
    text = file.read_text() if file.exists() else HEADER
    numbers = [int(n) for n in re.findall(r"^##\s+BUG-(\d+)", text, re.MULTILINE)]
    bug = Bug(id=f"BUG-{max(numbers, default=0) + 1}", status="open", reporter=reporter.strip() or "owner",
              when=time.strftime("%Y-%m-%d %H:%M"), title=title.strip().splitlines()[0],
              body=details.strip() if SEVERITY.search(details) else f"Severity: {severity}\n{details.strip()}".strip())
    entry = f"\n## {bug.id} · open · {bug.reporter} · {bug.when}\n{bug.title}\n" + (f"{bug.body}\n" if bug.body else "")
    file.write_text(text.rstrip("\n") + "\n" + entry)
    return bug


FORMAT = ("\nEach finding is a level-2 heading, appended at the end of this file (never rewrite the file):\n\n"
          "    ## BUG-<next number> · open · <reporter> · <YYYY-MM-DD HH:MM>\n    <one-line title>\n"
          "    Severity: blocker | major | minor\n    <where, the evidence, and what it should be>\n")


def ensure_format(project: Path) -> bool:
    """Give a findings file with no finding in LAW's format LAW's header (seen 2026-11-24: the first editor session
    made BUGS.md a Markdown table; later findings followed the table and LAW could not read them). True when reset."""
    file = path(project)
    text = file.read_text() if file.exists() else ""
    if parse(text) or text.startswith(HEADER):
        return False
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(HEADER + FORMAT)
    return True


def sections(text: str) -> dict[str, str]:
    """Each finding's raw text by its id."""
    heads = [m for m in re.finditer(r"^##\s+(BUG-\d+)\b.*$", text, re.MULTILINE)]
    return {m.group(1): text[m.start():heads[i + 1].start() if i + 1 < len(heads) else len(text)].rstrip() + "\n"
            for i, m in enumerate(heads)}


def restore_lost(project: Path, before: str) -> list[str]:
    """Put back findings an agent removed by rewriting the file (they are never deleted, only given a status).
    Returns the ids restored."""
    file = path(project)
    now = file.read_text() if file.exists() else HEADER
    lost = {k: v for k, v in sections(before).items() if k not in sections(now)}
    if lost:
        file.write_text(now.rstrip("\n") + "\n\n" + "\n".join(lost.values()))
    return sorted(lost)


UNFIXED_NOTE = ("LAW {date}: kept {status}: only a finding the fix stage marked fixed can be verified (the "
                "reviewer set verified on an unfixed finding). A finding that was wrong goes to the fix stage, which "
                "answers it and marks it fixed.")


def keep_unfixed(project: Path, before: str) -> list[str]:
    """Undo `verified` on a finding that was not `fixed` before this session (seen 2026-11-25: the editor verified
    six findings in its retests while the chapter had not changed since they were found). Returns the ids kept."""
    file = path(project)
    if not file.exists():
        return []
    was = {b.id: b.status for b in parse(before)}
    text, kept = file.read_text(), []
    for bug in parse(text):
        old = was.get(bug.id)
        if bug.status != "verified" or old in (None, "fixed", "verified"):
            continue
        head = re.compile(rf"^(##\s+{re.escape(bug.id)}\s*·\s*)verified(\s*·.*)$", re.MULTILINE)
        note = UNFIXED_NOTE.format(date=time.strftime("%Y-%m-%d %H:%M"), status=old)
        text = head.sub(rf"\g<1>{old}\g<2>", text, count=1)
        block = sections(text)[bug.id].rstrip("\n")
        text = text.replace(block, f"{block}\n{note}", 1)
        kept.append(bug.id)
    if kept:
        file.write_text(text)
    return kept


def as_issues(bugs: list[Bug]) -> list[str]:
    """Bugs in the shape the fix stage takes."""
    return [f"{b.id} ({b.status}, {b.severity}, found by {b.reporter}): {b.title} (details in .law/BUGS.md)"
            for b in bugs]
