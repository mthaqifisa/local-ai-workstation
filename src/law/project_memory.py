"""A project's working memory in `<project>/.law/`, like `.claude/` for Claude Code.

LAW writes the request and the translated brief; the agent keeps the research, plan and progress current as it
works. If an agent session dies halfway (worker restart, crash, stall), the next run reads `.law/` and continues
from the next unchecked step instead of starting over. `session.json` links a job to its OpenCode session so a
restarted job resumes the same session; it is machine-local and git-ignored. Nothing secret belongs in `.law/`.
"""

from __future__ import annotations

import json
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
- Before coding, record findings in {folder}/RESEARCH.md and numbered steps with checkboxes and gates in
  {folder}/PLAN.md.
- After every step, update {folder}/PROGRESS.md: what was done, the gate output, the exact next step. Tick the step
  in {folder}/PLAN.md.
- Never write secrets, keys or passwords into {folder}/."""
RESUME = ("Your previous session in this project was interrupted. Read {folder}/PROGRESS.md and {folder}/PLAN.md "
          "and continue from the next unchecked step. Keep {folder}/PROGRESS.md current.")


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
        (self.dir / ".gitignore").write_text("session.json\n")
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with (self.dir / "REQUESTS.md").open("a") as f:
            f.write(f"\n## {stamp} · job {job_id}\n\n{request.strip()}\n")
        (self.dir / "PROMPT.md").write_text(f"# Brief (job {job_id}, {stamp})\n\n{(brief or request).strip()}\n")

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
