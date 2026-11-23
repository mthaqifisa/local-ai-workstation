"""Self-improvement (owner 2026-11-23): Tony notices what LAW lacks and gets it built, tested and offered to the owner.

A proposal comes from Tony (the `propose_improvement` tool, when a missing skill, tool or rule made it fail or the
owner corrected the same thing twice) or from the owner (`/improve <what>`). LAW then builds it without asking: a
coder agent works in its own git worktree of the v6 repo on branch `improve/<id>`, follows CLAUDE.md (privacy, tests,
no secrets) and runs the unit tests, lint and types. Nothing reaches the running system until the owner taps Apply
on Telegram: Apply merges the branch, runs `law install` and pushes; Reject deletes the branch. Small fixes that
need no code (a missing Python package) heal themselves without this loop (law.calc.install_missing).
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from law.paths import LawPaths, display

STATES = ("building", "ready", "failed", "applied", "rejected")

BUILD_PROMPT = """You improve LAW itself (the local AI workstation that runs you). This folder is a git worktree of the
LAW v6 repository on branch {branch}; the live system is not touched until the owner applies your branch.

Read CLAUDE.md first and follow it exactly: the repo is public, so no personal data, usernames, home paths, hostnames
or secrets; paths in code are dynamic; production quality, typed, tested, no TODOs.

Problem: {problem}
What happened: {evidence}
Proposed change: {change}

Make the smallest change that fixes the problem, in the code, settings/roles.yaml or settings/registry.yaml. Add or
update unit tests that would have caught it. Then run, from this folder:
  ~/LAW/runtime/law-venv/bin/ruff check src tests scripts
  ~/LAW/runtime/law-venv/bin/mypy src
  PYTHONPATH=src ~/LAW/runtime/law-venv/bin/python -m pytest -q tests/unit   (PYTHONPATH: test this branch)
All three must pass. Commit on this branch (the repo already has the right commit identity). Do not push, merge, run
`law install` or restart services. If the change is unsafe or not worth it, commit nothing and say why.
Finish with: what changed and why, the files, and the three check results (copied from the real output)."""


@dataclass
class Proposal:
    id: str
    problem: str
    evidence: str
    change: str
    source: str  # "tony" or "owner"
    created: float
    status: str = "building"
    notified: str = ""  # the status the owner was last told about
    job: str = ""
    result: str = ""
    decided: float = 0.0

    @property
    def branch(self) -> str:
        return f"improve/{self.id}"


class Improvements:
    def __init__(self, paths: LawPaths) -> None:
        self.paths = paths
        self.dir = paths.data / "improve"

    def worktree(self, p: Proposal) -> Path:
        return self.paths.repo.parent / "v6-improve" / p.id

    def add(self, problem: str, evidence: str, change: str, source: str, now: float | None = None) -> Proposal:
        problem = " ".join(problem.split())
        if not problem or not change.strip():
            raise ValueError("a proposal needs the problem and the change")
        for p in self.all():  # the same problem twice while one is open is one proposal
            if p.problem.lower() == problem.lower() and p.status in ("building", "ready"):
                return p
        p = Proposal(secrets.token_hex(4), problem, " ".join(evidence.split()), " ".join(change.split()), source,
                     time.time() if now is None else now)
        self.save(p)
        return p

    def get(self, pid: str) -> Proposal:
        if not pid.isalnum() or len(pid) != 8:
            raise KeyError(pid)
        f = self.dir / f"{pid}.json"
        if not f.exists():
            raise KeyError(pid)
        return Proposal(**json.loads(f.read_text()))

    def all(self) -> list[Proposal]:
        out = []
        for f in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else []:
            try:
                out.append(Proposal(**json.loads(f.read_text())))
            except (OSError, ValueError, TypeError):
                continue
        return out

    def by_job(self, job: str) -> Proposal | None:
        return next((p for p in self.all() if p.job == job), None)

    def prepare(self, p: Proposal) -> Path:
        """A fresh worktree of the repo on the proposal's branch, for the coder agent."""
        wt = self.worktree(p)
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(self.paths.repo, "worktree", "add", "-b", p.branch, str(wt), "HEAD")
        return wt

    def built(self, p: Proposal, status: str, result: str) -> None:
        """The agent finished: ready when it committed something on the branch, else failed."""
        commits = _git(self.paths.repo, "rev-list", "--count", f"HEAD..{p.branch}", check=False).strip()
        p.status = "ready" if status == "done" and commits not in ("", "0") else "failed"
        p.result = result[-3000:]
        self.save(p)

    def apply(self, p: Proposal, now: float | None = None) -> None:
        if p.status != "ready":
            raise ValueError(f"proposal {p.id} is {p.status}, not ready")
        _git(self.paths.repo, "merge", "--no-edit", p.branch)
        self._cleanup(p)
        p.status, p.decided = "applied", time.time() if now is None else now
        self.save(p)

    def reject(self, p: Proposal, now: float | None = None) -> None:
        if p.status in ("applied", "rejected"):
            raise ValueError(f"proposal {p.id} is already {p.status}")
        self._cleanup(p)
        p.status, p.decided = "rejected", time.time() if now is None else now
        self.save(p)

    def to_notify(self) -> list[Proposal]:
        return [p for p in self.all() if p.notified != p.status and p.status in ("ready", "failed")]

    def mark_notified(self, p: Proposal) -> None:
        p.notified = p.status
        self.save(p)

    def _cleanup(self, p: Proposal) -> None:
        wt = self.worktree(p)
        if wt.exists():
            _git(self.paths.repo, "worktree", "remove", "--force", str(wt), check=False)
        _git(self.paths.repo, "branch", "-D", p.branch, check=False)

    def save(self, p: Proposal) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{p.id}.tmp"
        tmp.write_text(json.dumps(asdict(p), indent=1))
        tmp.replace(self.dir / f"{p.id}.json")


def build_payload(p: Proposal, folder: Path) -> dict[str, Any]:
    return {"role": "coder", "dir": str(folder), "improvement": p.id,
            "prompt": BUILD_PROMPT.format(branch=p.branch, problem=p.problem, evidence=p.evidence or "-",
                                          change=p.change)}


def message(p: Proposal, paths: LawPaths) -> str:
    head = {"ready": "🔧 Improvement built and tested", "failed": "🔧 Improvement not built"}.get(p.status, "🔧")
    who = "Tony noticed" if p.source == "tony" else "You asked"
    text = (f"{head} ({p.id})\n{who}: {p.problem}\nChange: {p.change}\n\n{p.result.strip()[-1800:]}")
    if p.status == "ready":
        text += f"\n\nBranch {p.branch} in {display(paths.repo)}. Apply merges it, reinstalls and pushes."
    return text


def _git(repo: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()[-500:]}")
    return res.stdout
