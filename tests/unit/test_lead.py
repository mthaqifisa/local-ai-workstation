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
    assert leads.pending_for("j1", d.ticket) is None


def test_a_draft_waits_for_the_owner_with_no_time_limit(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    d = _draft(leads, project, now=1000.0)
    done = leads.decide(d.id, "approved", now=1000.0 + 7 * 24 * 3600)  # a week later, the ticket still open
    assert done is not None and done.status == "approved"
    assert d.note in (ProjectMemory(project).take_notes() or "")


def test_an_answer_after_the_ticket_is_done_is_outdated_and_not_sent(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    (project / ".law" / "PLAN.md").write_text("# Plan\n- [x] P-22.6 contrast tokens\n- [ ] P-22.7 next\n")
    d = _draft(leads, project)
    done = leads.decide(d.id, "edited", "Write the tokens test first.")
    assert done is not None and done.status == "outdated" and done.final_note == ""
    assert ProjectMemory(project).take_notes() is None
    assert leads.outcomes()[-1]["decision"] == "outdated"
    d2 = _draft(leads, project, job="j2")
    assert leads.decide(d2.id, "dropped") is not None and leads.get(d2.id).status == "dropped"  # type: ignore[union-attr]


def test_ticket_done_reads_the_ticked_plan_line(tmp_path: Path) -> None:
    (tmp_path / ".law").mkdir()
    (tmp_path / ".law" / "PLAN.md").write_text("- [x] DBTC-11 7.2 research\n- [ ] DBTC-12 7.2 prose\n")
    assert lead.ticket_done(tmp_path, "DBTC-11 7.2 research (acceptance: ...)")
    assert not lead.ticket_done(tmp_path, "DBTC-12 7.2 prose")
    assert not lead.ticket_done(tmp_path, "DBTC-1 metadata")  # DBTC-1 is not DBTC-11
    assert not lead.ticket_done(tmp_path / "missing", "DBTC-11 x")


def test_one_pending_draft_per_ticket_and_sent_once(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    d = _draft(leads, project)
    assert leads.pending_for("j1", d.ticket) == d and leads.pending_for("j2", d.ticket) is None
    assert leads.pending_for("j1", "P-22.7 next ticket") is None  # an unanswered draft never blocks the next ticket
    leads.mark_sent(d)
    assert leads.unsent() == []


def test_approval_rate_lands_in_progress_every_ten(setup: tuple[lead.Leads, Path]) -> None:
    leads, project = setup
    for i in range(10):
        d = _draft(leads, project, job=f"j{i}")
        leads.decide(d.id, "approved" if i < 6 else "dropped")
    text = (leads.paths.repo / "PROGRESS.md").read_text()
    assert "Lead notes: 10 decided, 60% used (6 approved, 0 edited, 4 dropped, 0 answered after the ticket, 0 unanswered)" in text


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


def test_the_lead_sees_the_plan_as_it_stands_now(tmp_path: Path) -> None:
    (tmp_path / ".law").mkdir()
    (tmp_path / ".law" / "PLAN.md").write_text("- [x] B-21 diagram\n- [ ] B-22.1 batch job in code/eod-batch\n"
                                               "- [ ] B-22.2 chapter text\n")
    text = lead.context_message("B-22 Java batch job", ["12:00 reading"], tmp_path, now="12:30")
    assert "B-22.1 batch job in code/eod-batch" in text and "B-21 diagram" not in text
    assert lead.open_tickets(tmp_path / "missing") == "(no plan)"
