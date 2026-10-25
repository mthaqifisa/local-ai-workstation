"""A project's bug list in `<project>/.law/BUGS.md`: what QA and the owner find in the product, and where each bug is.

One file is the single source for the QA agent (files and retests bugs), the coder (fixes them), Telegram
(`/bugfix` reports one) and the Console board (a ticket per bug). Each bug is a level-2 heading followed by its
report:

    ## BUG-3 · open · owner · 2026-10-26 07:10
    Scanning a known barcode twice shows no "already at home" alert
    Steps: ... Expected: ... Actual: ...

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


def add(project: Path, title: str, details: str = "", *, reporter: str = "owner") -> Bug:
    """Append a new open bug and return it (ids are never reused)."""
    if not title.strip():
        raise ValueError("describe the bug")
    file = path(project)
    file.parent.mkdir(parents=True, exist_ok=True)
    text = file.read_text() if file.exists() else HEADER
    numbers = [int(n) for n in re.findall(r"^##\s+BUG-(\d+)", text, re.MULTILINE)]
    bug = Bug(id=f"BUG-{max(numbers, default=0) + 1}", status="open", reporter=reporter.strip() or "owner",
              when=time.strftime("%Y-%m-%d %H:%M"), title=title.strip().splitlines()[0], body=details.strip())
    entry = f"\n## {bug.id} · open · {bug.reporter} · {bug.when}\n{bug.title}\n" + (f"{bug.body}\n" if bug.body else "")
    file.write_text(text.rstrip("\n") + "\n" + entry)
    return bug


def as_issues(bugs: list[Bug]) -> list[str]:
    """Bugs in the shape the fix stage takes."""
    return [f"{b.id} ({b.status}, found by {b.reporter}): {b.title} (details in .law/BUGS.md)" for b in bugs]
