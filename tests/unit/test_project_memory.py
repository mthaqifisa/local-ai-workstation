"""Project working memory in <project>/.law/ and the translator stage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from law.project_memory import FOLDER, SEEDS, ProjectMemory, project_listing


def test_prepare_seeds_files_once_and_logs_every_request(tmp_path: Path) -> None:
    mem = ProjectMemory(tmp_path)
    mem.prepare("job1", "build a chat app", "## Goal\nA LAN chat app")
    (tmp_path / FOLDER / "PLAN.md").write_text("# Plan\n- [x] backend\n- [ ] frontend\n")
    mem.prepare("job2", "add dark mode", None)
    law = tmp_path / FOLDER
    assert (law / "PLAN.md").read_text().endswith("- [ ] frontend\n")  # agent's plan is never overwritten
    assert "build a chat app" in (law / "REQUESTS.md").read_text() and "add dark mode" in (law / "REQUESTS.md").read_text()
    assert "add dark mode" in (law / "PROMPT.md").read_text()  # no brief: the request itself
    ignore = (law / ".gitignore").read_text()
    assert ignore.startswith("*\n") and "!PLAN.md\n" in ignore and "session.json" not in ignore  # allow-list
    assert set(SEEDS) <= {p.name for p in law.iterdir()}


def test_the_brief_is_rewritten_only_when_it_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-26 to -29: a fresh timestamp each stage left PROMPT.md always modified; agents diffed it 35 times."""
    import law.project_memory as pm

    mem = ProjectMemory(tmp_path)
    monkeypatch.setattr(pm.time, "strftime", lambda fmt: "2026-10-28 10:00")
    mem.prepare("root", "build pantry", "## Goal\nPantry")
    prompt = tmp_path / FOLDER / "PROMPT.md"
    first = prompt.read_text()
    monkeypatch.setattr(pm.time, "strftime", lambda fmt: "2026-10-29 09:00")
    mem.prepare("root", "build pantry", "## Goal\nPantry")  # the next stage of the same pipeline
    assert prompt.read_text() == first
    mem.prepare("root", "build pantry", "## Goal\nPantry v2")
    assert "Pantry v2" in prompt.read_text()


def test_context_shows_only_real_plan_and_progress(tmp_path: Path) -> None:
    mem = ProjectMemory(tmp_path)
    mem.prepare("j", "x", None)
    assert mem.context() == ""
    (tmp_path / FOLDER / "PROGRESS.md").write_text("# Progress\nbackend green; next: frontend\n")
    assert "next: frontend" in mem.context() and "PLAN.md" not in mem.context()


def test_session_is_resumed_only_by_the_same_job(tmp_path: Path) -> None:
    mem = ProjectMemory(tmp_path)
    assert mem.session_for("j1") is None
    mem.save_session("j1", "coder", "ses_1")
    assert mem.session_for("j1") == "ses_1" and mem.session_for("j2") is None
    assert json.loads((tmp_path / FOLDER / "session.json").read_text())["role"] == "coder"
    (tmp_path / FOLDER / "session.json").write_text("not json")
    assert mem.session_for("j1") is None


def test_listing_skips_hidden_and_build_folders(tmp_path: Path) -> None:
    for rel in ("backend/pom.xml", "backend/target/app.jar", "frontend/node_modules/x/y.js", ".git/HEAD",
                "README.md", "backend/src/main/App.java"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    listing = project_listing(tmp_path).splitlines()
    assert "backend/pom.xml" in listing and "README.md" in listing and "backend/src/" in listing
    assert not any("target" in ln or "node_modules" in ln or ".git" in ln or "App.java" in ln for ln in listing)
    assert project_listing(tmp_path / "backend" / "src" / "main" / "App.java".replace("App.java", "")) != ""


def test_law_records_a_step_the_agent_did_not(tmp_path: Path) -> None:
    import os
    import time

    mem = ProjectMemory(tmp_path)
    mem.prepare("j", "x", None)
    started = time.time()
    progress = tmp_path / FOLDER / "PROGRESS.md"
    os.utime(progress, (started - 60, started - 60))  # untouched during the step
    assert mem.ensure_progress(started, "writer", "Wrote sections 1-3.", ["docs/proposal/proposal.md"] * 2)
    text = progress.read_text()
    assert "writer step" in text and "Wrote sections 1-3." in text and text.count("proposal.md") == 1
    assert not mem.ensure_progress(started, "writer", "again", [])  # the file is now newer than the step start


def test_owner_notes_are_delivered_once_and_recorded(tmp_path: Path) -> None:
    mem = ProjectMemory(tmp_path)
    mem.prepare("j", "x", None)
    assert mem.take_notes() is None
    mem.add_note("Spring Boot reads PEM files directly")
    mem.add_note("keep the p12 out of .law")
    note = mem.take_notes()
    assert note is not None and "PEM files" in note and "p12" in note
    assert mem.take_notes() is None  # once
    assert "owner note to the agent" in (tmp_path / FOLDER / "REQUESTS.md").read_text()


def test_a_session_cannot_remove_the_board_tickets(tmp_path: Path) -> None:
    memory = ProjectMemory(tmp_path)
    memory.prepare("j1", "r", None)
    plan = tmp_path / ".law" / "PLAN.md"
    before = "# Plan\n\n## Build version 1\n\n### M1 API\n- [x] P-1 Health\n\n## Later\nnotes\n"
    plan.write_text("# PLAN: icons\n- [ ] 1 buttons\n")
    assert memory.keep_build_sections(before)
    assert "- [x] P-1 Health" in plan.read_text() and "1 buttons" in plan.read_text()
    assert not memory.keep_build_sections(before)  # already there: a split inside the section changes nothing


def test_pending_notes_show_until_an_agent_takes_them(tmp_path: Path) -> None:
    memory = ProjectMemory(tmp_path)
    memory.prepare("j1", "r", None)
    assert memory.pending_notes() == ""
    memory.add_note("use icons")
    assert memory.pending_notes() == "use icons"
    memory.take_notes()
    assert memory.pending_notes() == ""


def test_a_note_on_a_finished_ticket_becomes_the_next_ticket(tmp_path):
    from law import board

    law = tmp_path / ".law"
    law.mkdir()
    (law / "PLAN.md").write_text("# Plan\n\n## Build version 1\n\n### M1 Part one\n- [x] DB-1 Chapter 1 (acceptance: x)\n"
                                 "- [ ] DB-2 Chapter 2 (acceptance: y)\n- [ ] DB-3 Chapter 3 (acceptance: z)\n")
    assert board.deliver_note(tmp_path, "DB-1", "the ledger diagram is wrong") == "ticket DB-4 added after the current one"
    lines = (law / "PLAN.md").read_text().splitlines()
    i = lines.index("- [ ] DB-2 Chapter 2 (acceptance: y)")
    assert lines[i + 1].startswith("- [ ] DB-4 Revise DB-1 as the owner noted: the ledger diagram is wrong")
    assert "then do DB-4" in (law / "NOTES.md").read_text()
    assert board.deliver_note(tmp_path, "DB-2", "use RM, not USD") == "the agent gets it at its next action"
    assert "(on DB-2) use RM, not USD" in (law / "NOTES.md").read_text()
