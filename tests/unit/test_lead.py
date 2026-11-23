"""The lead step (ADR-0026): drafts reach the agent only when the owner approves or edits them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from law import lead
from law.paths import LawPaths
from law.project_memory import ProjectMemory


@pytest.fixture
def setup(tmp_path: Path) -> tuple[lead.Leads, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "PROGRESS.md").write_text("# Progress\n")
    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    return lead.Leads(LawPaths(repo=repo, home=tmp_path / "LAW", user_home=tmp_path)), project


def _draft(leads: lead.Leads, project: Path, job: str = "j1", now: float = 1000.0) -> lead.Draft:
    return leads.add(project="pantry", folder=project, ticket="P-22.6 contrast tokens", job=job,
                     diagnosis="testing CSS cascade in jsdom", note="Stop testing the cascade; test the tokens.",
                     now=now)


def test_approve_sends_the_draft_as_the_note(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    d = _draft(leads, project)
    assert leads.unsent() == [d]
    done = leads.decide(d.id, "approved")
    assert done is not None and done.final_note == d.note
    assert d.note in (ProjectMemory(project).take_notes() or "")
    assert leads.decide(d.id, "dropped") is None  # decided once
    assert [o["decision"] for o in leads.outcomes()] == ["approved"]


def test_edit_sends_the_owners_words_not_the_draft(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    d = _draft(leads, project)
    with pytest.raises(ValueError):
        leads.decide(d.id, "edited", "  ")
    leads.decide(d.id, "edited", "Write a11y.test.tsx as a new small file.")
    note = ProjectMemory(project).take_notes() or ""
    assert "a11y.test.tsx" in note and d.note not in note


def test_drop_sends_nothing(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    d = _draft(leads, project)
    leads.decide(d.id, "dropped")
    assert ProjectMemory(project).take_notes() is None
    assert leads.pending_for("j1") is None


def test_timeout_counts_as_drop(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    d = _draft(leads, project, now=1000.0)
    assert leads.expire(now=1000.0 + lead.DECIDE_TIMEOUT_S - 1) == []
    gone = leads.expire(now=1000.0 + lead.DECIDE_TIMEOUT_S)
    assert [g.id for g in gone] == [d.id] and gone[0].status == "expired"
    assert ProjectMemory(project).take_notes() is None


def test_one_pending_draft_per_job_and_sent_once(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    d = _draft(leads, project)
    assert leads.pending_for("j1") == d and leads.pending_for("j2") is None
    leads.mark_sent(d)
    assert leads.unsent() == []


def test_approval_rate_lands_in_progress_every_ten(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    for i in range(10):
        d = _draft(leads, project, job=f"j{i}")
        leads.decide(d.id, "approved" if i < 6 else "dropped")
    text = (leads.paths.repo / "PROGRESS.md").read_text()
    assert "Lead notes: 10 decided, 60% used (6 approved, 0 edited, 4 dropped, 0 unanswered)" in text


def test_bad_ids_and_decisions(setup: tuple[lead.Leads, Path]) -> None:
    leads, _ = setup
    assert leads.get("../../etc") is None and leads.decide("deadbeef", "approved") is None
    with pytest.raises(ValueError):
        leads.decide("deadbeef", "maybe")


@pytest.mark.parametrize(("answer", "want"), [
    ('{"stuck": true, "diagnosis": "d", "note": "n"}', (True, "d", "n")),
    ('thinking... {"stuck": true, "diagnosis": "d", "note": ""}', (False, "d", "")),  # stuck but no note: none
    ('{"stuck": false, "diagnosis": "fine", "note": ""}', (False, "fine", "")),
    ("NO NOTE", (False, "", "")),
    ('{"stuck": tru', (False, "", "")),
])
def test_parse(answer: str, want: tuple[bool, str, str]) -> None:
    assert lead.parse(answer) == want


def test_context_lists_files_by_size_without_law_folder(setup: tuple[lead.Leads, Path]) -> None:
    _, project = setup
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "big.ts").write_text("x" * 5000)
    (project / "small.ts").write_text("y")
    (project / ".law" / "PLAN.md").write_text("plan")
    text = lead.context_message("P-1 do it", ["10:00 📖 read big.ts"], project, now="10:30")
    files = text.split("Project files (bytes, path):\n", 1)[1]
    assert files.index("big.ts") < files.index("small.ts") and ".law/" not in files
    assert "10:00 📖 read big.ts" in text and "Now is 10:30." in text
