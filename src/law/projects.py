"""Projects driven end to end from Telegram, with the owner deciding at each gate.

`/code init` sends a template; the filled template starts a project. Its life:

    proposing ──(proposal emailed)──▶ awaiting_go ──go──▶ developing ──(green + reviewed)──▶ awaiting_verification
        ▲                                │ no go + what to change                                │      │
        └────────────────────────────────┘ (revised proposal, next version)                    │      all good ──▶ done
        ▲                                                                                       │
        └──────────────────────────── needs changes + what to change (enhanced proposal) ──────┘

`no go` without a note parks the project; `resume <note>` revives it. The record lives in the project folder
(`<project>/.law/project.json`, committed with the code); `~/LAW/state/projects.json` maps names to folders.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from law.paths import LawPaths

State = Literal["proposing", "awaiting_go", "developing", "awaiting_verification", "parked", "done"]
Action = Literal["go", "nogo", "changes", "allgood", "resume", "push"]
EFFORTS = {"low": "low", "medium": "medium", "high": "xhigh"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
URL_RE = re.compile(r"^(https://[^\s@/:]+(?::\d+)?/[^\s@]+|git@[^\s:]+:[^\s]+)$")

TEMPLATE_INTRO = ("Copy the next message, fill it in and send it back. Required: **name**, **summary**, **details**. "
                  "Optional: **repo** (folder under ~/SourceCode; from the name if empty), **repo_url** (an existing "
                  "git repo to clone, or the remote for a new one), **stack** (empty: LAW recommends one), "
                  "**thinking** (low, medium or high; default medium: how hard the coder thinks per step).")
TEMPLATE = """/code new
name:
repo:
repo_url:
stack:
thinking: medium
summary:
details:
"""
FIELDS = ("name", "repo", "repo_url", "stack", "thinking", "summary", "details")


class TemplateError(ValueError):
    pass


class Event(BaseModel):
    at: str
    event: str
    note: str = ""


class Project(BaseModel):
    id: str
    name: str
    dir: str
    repo_url: str = ""
    stack: str = ""
    thinking: str = "medium"
    summary: str
    details: str
    state: State = "proposing"
    version: int = 1
    feedback: str = ""  # what the owner asked to change for the current version
    chat: int | None = None  # Telegram chat that receives the gates
    history: list[Event] = Field(default_factory=list)

    @property
    def effort(self) -> str:
        return EFFORTS.get(self.thinking, "medium")

    def log(self, event: str, note: str = "") -> None:
        self.history.append(Event(at=time.strftime("%Y-%m-%dT%H:%M:%S"), event=event, note=note))

    def request(self) -> str:
        """The requirements as one text, for the translator and the writers."""
        parts = [f"Project: {self.name}", f"Summary: {self.summary}",
                 f"Stack: {self.stack or 'recommend the best fit and justify it'}", f"Details:\n{self.details}"]
        if self.feedback:
            parts.append(f"Owner feedback for version {self.version}:\n{self.feedback}")
        return "\n".join(parts)


def parse_template(text: str) -> dict[str, str]:
    """Read `key: value` lines; `details:` takes everything after it. Raises TemplateError with what is wrong."""
    lines = text.strip().splitlines()
    if lines and lines[0].strip().lower().startswith("/code"):
        lines = lines[1:]
    fields: dict[str, str] = {}
    for i, line in enumerate(lines):
        key, sep, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        if not sep or key not in FIELDS:
            continue
        if key == "details":
            fields[key] = "\n".join([value.strip(), *lines[i + 1:]]).strip()
            break
        fields[key] = value.strip()
    missing = [k for k in ("name", "summary", "details") if not fields.get(k)]
    if missing:
        raise TemplateError(f"please fill in: {', '.join(missing)}")
    thinking = (fields.get("thinking") or "medium").lower()
    if thinking not in EFFORTS:
        raise TemplateError("thinking must be low, medium or high")
    fields["thinking"] = thinking
    repo = (fields.get("repo") or slugify(fields["name"])).lower()
    if not NAME_RE.match(repo):
        raise TemplateError("repo must be a folder name: letters, numbers, - . _ (e.g. office-chat)")
    fields["repo"] = repo
    url = fields.get("repo_url", "")
    if url and not URL_RE.match(url):
        raise TemplateError("repo_url must be https://host/owner/repo or git@host:owner/repo, without a password "
                            "or token in it")
    return fields


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "project"


class Projects:
    """Project records: `<project>/.law/project.json`, indexed by id in ~/LAW/state/projects.json."""

    def __init__(self, paths: LawPaths) -> None:
        self.paths = paths
        self.index_file = paths.state / "projects.json"

    def _index(self) -> dict[str, str]:
        try:
            data = json.loads(self.index_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, ref: str) -> Project:
        """By id, or by a unique prefix / folder name, as typed in Telegram."""
        index = self._index()
        ref = ref.strip().lower()
        matches = [k for k in index if k == ref] or [k for k in index if k.startswith(ref)]
        if len(matches) != 1:
            known = ", ".join(sorted(index)) or "none yet"
            raise KeyError(f"no single project matches {ref!r} (projects: {known})")
        record = Path(index[matches[0]]) / ".law" / "project.json"
        return Project.model_validate_json(record.read_text())

    def all(self) -> list[Project]:
        out = []
        for pid in sorted(self._index()):
            try:
                out.append(self.get(pid))
            except (KeyError, OSError, ValueError):
                continue
        return out

    def save(self, project: Project) -> None:
        folder = Path(project.dir) / ".law"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "project.json").write_text(project.model_dump_json(indent=2) + "\n")
        index = self._index()
        index[project.id] = project.dir
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")

    def create(self, fields: dict[str, str], chat: int | None) -> Project:
        folder = self.paths.projects / fields["repo"]
        if fields["repo"] in self._index():
            raise TemplateError(f"a project named {fields['repo']} already exists; pick another repo name")
        project = Project(id=fields["repo"], name=fields["name"], dir=str(folder),
                          repo_url=fields.get("repo_url", ""), stack=fields.get("stack", ""),
                          thinking=fields["thinking"], summary=fields["summary"], details=fields["details"], chat=chat)
        project.log("created")
        return project


ALLOWED: dict[str, set[str]] = {
    "awaiting_go": {"go", "nogo"},
    "awaiting_verification": {"allgood", "changes"},
    "parked": {"resume"},
    "done": {"push", "changes"},
}


def decide(project: Project, action: str, note: str = "") -> str:
    """Apply the owner's decision. Returns the next phase to run: 'proposal', 'develop', 'push' or '' (none).
    Raises ValueError when the action doesn't fit the project's state."""
    if action not in ALLOWED.get(project.state, set()):
        allowed = ", ".join(sorted(ALLOWED.get(project.state, set()))) or "nothing (it is working)"
        raise ValueError(f"{project.name} is {project.state.replace('_', ' ')}; you can: {allowed}")
    if action == "push" and not project.repo_url:
        raise ValueError(f"{project.name} has no repo_url to push to")
    if action in {"changes", "resume"} and not note.strip():
        raise ValueError("say what should change")
    project.log(action, note)
    if action == "go":
        project.state = "developing"
        return "develop"
    if action == "allgood":
        project.state = "done"
        return ""
    if action == "push":
        return "push"
    if action in {"nogo", "changes", "resume"}:
        if not note.strip():  # a no go without notes parks the project
            project.state = "parked"
            return ""
        # every revision is a new proposal version (v2, v3, …) with a "what changed" section
        project.version += 1
        project.feedback = note.strip()
        project.state = "proposing"
        return "proposal"
    raise ValueError(f"unknown action {action!r}")
