"""Project working memory in <project>/.law/ and the translator stage."""

from __future__ import annotations

import json
from pathlib import Path

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
    assert (law / ".gitignore").read_text() == "session.json\n"
    assert set(SEEDS) <= {p.name for p in law.iterdir()}


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
