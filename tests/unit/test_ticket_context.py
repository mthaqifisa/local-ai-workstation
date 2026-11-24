"""Ticket sessions get LAW's copy of the brief, milestone, latest progress and style (2026-11-24: 5-7 min a ticket
went to re-reading .law/ files)."""

from __future__ import annotations

from pathlib import Path

from law.handlers.tasks import milestone_board, ticket_context

PLAN = """# Plan

## Build version 1

### M1 Scaffold
- [x] K-1 Kit
- [ ] K-2 Title page [est 20m]

### M2 Chapter one
- [ ] K-3 Research chapter 1
"""

PROGRESS = """# Progress

Newest first.

## 2026-11-24 K-2 title page
Done: cover.
## 2026-11-24 K-1 kit
Done: kit.
## 2026-11-23 env
Done: tools.
"""


def _project(tmp_path: Path, style: bool = True) -> Path:
    law = tmp_path / ".law"
    law.mkdir()
    (law / "PLAN.md").write_text(PLAN)
    (law / "PROGRESS.md").write_text(PROGRESS)
    (law / "PROMPT.md").write_text("# Brief\n\nWrite chapter 1.")
    if style:
        (law / "STYLE.md").write_text("# Style\n\nCite everything.")
    return tmp_path


def test_milestone_board_is_only_that_milestone(tmp_path: Path) -> None:
    board = milestone_board(_project(tmp_path), 1, "M1 Scaffold")
    assert "K-1 Kit" in board and "K-2 Title page" in board and "K-3" not in board


def test_context_has_brief_milestone_latest_progress_and_style(tmp_path: Path) -> None:
    ctx = ticket_context(_project(tmp_path), 1, "M2 Chapter one")
    assert "Write chapter 1." in ctx and "K-3 Research chapter 1" in ctx and "K-1 Kit" not in ctx
    assert "K-2 title page" in ctx and "K-1 kit" in ctx and "env" not in ctx.split("PROGRESS.md, latest")[1]
    assert "Cite everything." in ctx and "do not open PROMPT.md" in ctx


def test_long_files_are_cut(tmp_path: Path) -> None:
    project = _project(tmp_path, style=False)
    (project / ".law" / "PROMPT.md").write_text("line\n" * 5000)
    ctx = ticket_context(project, 1, "M1 Scaffold")
    assert "[... cut]" in ctx and "STYLE.md ---" not in ctx


def test_no_memory_no_context(tmp_path: Path) -> None:
    assert ticket_context(tmp_path, 1, "M1") == ""
