"""Project folders stay clean.

Agents keep their per-project state — task specs, delegation briefs, raw run logs and
memory — in the central store (law/runtime/store.py), keyed by the project's path, NOT in
the project folder. Undo snapshots live under ~/.law/state/undo/. So a normal agent run
adds NOTHING to your project.

The only project file law ever reads is an OPTIONAL <project>/.local-agent/AGENT.md with
your own instructions for agents working here. It is read if present and created only when
you ask for it with `law init-project`.
"""
from __future__ import annotations

from pathlib import Path

AGENT_MD = """# Agent Instructions

This file is loaded automatically when the local agent runs in this project.
Edit it to give the agent project-specific context, constraints, and preferences.
(It is the only file law keeps in your project; everything else lives in law's store.)

## Project Overview
<!-- One paragraph: what this project is, who it's for, what success looks like -->

## Tech Stack
<!-- Key technologies, frameworks, and versions in use -->

## Agent Rules
<!-- Project-specific rules: port numbers, file conventions, forbidden actions, etc. -->

## Key Files & Paths
<!-- Important files the agent should know about before touching anything -->
"""


def ensure_project(project_dir: Path) -> Path:
    """Resolve and create the project folder (and its parents). Writes NO files inside it.
    Raises NotADirectoryError if the path exists but is not a folder."""
    p = Path(project_dir).expanduser().resolve()
    if p.exists() and not p.is_dir():
        raise NotADirectoryError(f"not a folder: {p}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def scaffold(project_dir: Path) -> tuple[Path, list[str]]:
    """`law init-project`: create the optional <project>/.local-agent/AGENT.md template for
    you to fill in. Returns (.local-agent path, list of created items). Idempotent."""
    p = ensure_project(project_dir)
    la = p / ".local-agent"
    la.mkdir(exist_ok=True)
    created: list[str] = []
    f = la / "AGENT.md"
    if not f.exists():
        f.write_text(AGENT_MD)
        created.append("AGENT.md")
    return la, created


# Back-compat alias: the many call sites that just need the folder to exist.
init_project = ensure_project
