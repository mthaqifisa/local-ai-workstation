"""Lessons carried from one project to the next (owner 2026-11-24: "the next project won't take this much time?").

An agent that solves a problem other projects will hit (a tool's quirk, a version that breaks, the command that
works) writes it to the project's `.law/LESSONS.md` as

    ## <one line: the problem>
    tags: <tools and versions, e.g. typst 0.15, pandoc 3.10>
    <what happened and what works, a few lines>

LAW takes an entry only from a ticket whose gate passed with it, so a lesson is proven in at least one project,
and keeps it in `~/LAW/data/lessons/<role>.md` (machine-local: lessons can name local paths). Every later session of
that role gets the newest lessons, within LIMIT characters: a few hundred words, not a library, because a long
prompt slows the local model and blurs its attention.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from law.paths import LawPaths

FILE = "LESSONS.md"
LIMIT = 3000  # characters of lessons in a prompt
ENTRY_MAX = 900  # one lesson is a few lines; a longer one is cut
ENTRY = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
HEAD = ("Lessons from earlier projects (each one worked there; check the versions in its tags against this "
        "project before relying on it):")
INSTRUCTION = ("When you solve a problem another project will hit too (a tool's quirk, a version that breaks, a "
               "command that works), add it to .law/LESSONS.md: `## <the problem in one line>`, then "
               "`tags: <tools and versions>`, then what works in a few lines. Only what you verified.")


@dataclass(frozen=True)
class Lesson:
    title: str
    body: str

    @property
    def key(self) -> str:
        return " ".join(self.title.lower().split())

    def text(self) -> str:
        return f"## {self.title}\n{self.body.strip()}\n"


def parse(text: str) -> list[Lesson]:
    out = []
    heads = list(ENTRY.finditer(text))
    for n, m in enumerate(heads):
        end = heads[n + 1].start() if n + 1 < len(heads) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.append(Lesson(m.group(1).strip(), body[:ENTRY_MAX]))
    return out


class Lessons:
    def __init__(self, paths: LawPaths) -> None:
        self.dir = paths.data / "lessons"

    def file(self, role: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", role):
            raise ValueError(f"bad role {role!r}")
        return self.dir / f"{role}.md"

    def all(self, role: str) -> list[Lesson]:
        f = self.file(role)
        return parse(f.read_text()) if f.is_file() else []

    def harvest(self, project: Path, role: str) -> list[str]:
        """Take the project's new lessons (call only after its gate passed). Returns the titles added."""
        source = project / ".law" / FILE
        if not source.is_file():
            return []
        known = {lesson.key for lesson in self.all(role)}
        new = [lesson for lesson in parse(source.read_text()) if lesson.key not in known]
        if not new:
            return []
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d")
        with self.file(role).open("a") as f:
            for lesson in new:
                f.write(f"\n{lesson.text().rstrip()}\nfrom: {project.name}, {stamp}\n")
        return [lesson.title for lesson in new]

    def prompt(self, role: str) -> str:
        """The newest lessons that fit LIMIT, newest first, under HEAD ("" when there are none)."""
        picked, size = [], len(HEAD)
        for lesson in reversed(self.all(role)):
            text = lesson.text()
            if size + len(text) > LIMIT:
                break
            picked.append(text)
            size += len(text)
        return f"{HEAD}\n\n" + "\n".join(picked) if picked else ""
