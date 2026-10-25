"""Project board (the Console Projects tab): tickets from pipeline stages, gates and PLAN.md; notes; deleting a project."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from law import board
from law.api import create_app
from law.paths import LawPaths
from law.projects import Project, Projects
from law.queue.jobs import Job, Status
from law.queue.store import JobStore

REPO = Path(__file__).resolve().parents[2]
PLAN = """# Plan

## Build version 1

### M1 Backend
- [x] P-1 Skeleton with health endpoint.
      Acceptance: mvn verify green.
- [ ] P-2 Products API
      (validation, 409 on duplicates). Acceptance: ITs.
Milestone gate: verify green.

### M2 UI
- [ ] P-3 Scan screen
"""


def _paths(sandbox: Path, tmp_path: Path) -> LawPaths:
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "settings", repo / "settings")
    return LawPaths(repo=repo, home=sandbox / "LAW", user_home=sandbox)


def _project(paths: LawPaths, state: str = "developing") -> Project:
    folder = paths.projects / "pantry"
    (folder / ".law").mkdir(parents=True)
    (folder / ".law" / "PLAN.md").write_text(PLAN)
    (folder / ".law" / "PROGRESS.md").write_text("# Progress\n\n## 2026-10-25 10:00 · P-1 done\nhealth green\n")
    project = Project(id="pantry", name="Pantry", dir=str(folder), summary="s", details="d", state=state)  # type: ignore[arg-type]
    project.log("created")
    Projects(paths).save(project)
    return project


def _specs() -> list[dict[str, Any]]:
    base = {"project": "pantry", "version": 1}
    return [{"kind": "task_work", "payload": {**base, "spec_ref": "project.env"}},
            {"kind": "task_work", "payload": {**base, "spec_ref": "project.dev", "milestone": True}},
            {"kind": "task_review", "payload": {**base, "spec_ref": "project.fix"}},
            {"kind": "task_fix", "payload": {**base, "spec_ref": "project.fix"}},
            {"kind": "project_gate", "payload": {"project": "pantry", "gate": "verify"}}]


async def _stage(store: JobStore, root: Job, index: int, status: Status, **extra: Any) -> Job:
    spec = root.payload["_stages"][index]
    job = Job(kind=spec["kind"], payload={**spec["payload"], **extra.pop("payload", {})}, parent=root.id,
              stage=index, status=status, started=time.time() - 600 + index, **extra)
    await store.save(job)
    await store.r.zadd("law:history", {job.id: job.created})
    return job


def test_plan_tickets_read_milestones_ids_and_details():
    items = board.plan_tickets(PLAN.split("## Build version 1", 1)[1])
    assert [(t["id"], t["done"], t["milestone"]) for t in items] == [
        ("P-1", True, "M1 Backend"), ("P-2", False, "M1 Backend"), ("P-3", False, "M2 UI")]
    assert items[1]["title"] == "Products API (validation, 409 on duplicates)"  # title stops at the acceptance
    assert items[1]["details"].endswith("Acceptance: ITs.")
    assert "Milestone gate" not in items[1]["details"]


async def test_board_columns_from_stages_plan_and_notes(sandbox: Path, tmp_path: Path):
    paths = _paths(sandbox, tmp_path)
    _project(paths)
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    root = await store.submit(Job(kind="project_step", payload={"project": "pantry", "_stages": _specs()}))
    await store.mark_waiting(root)
    await _stage(store, root, 0, Status.DONE, finished=time.time() - 500)
    dev = await _stage(store, root, 1, Status.RUNNING)
    await store.event(dev.id, "progress", {"message": "✏️ editing ProductController.java"})
    board.add_note(paths, "pantry", "use 409 for duplicates")  # no ticket: pinned to the one in progress
    board.add_note(paths, "pantry", "QA must check ports", ticket="qa-v1-1", author="Claude")

    data = await board.build(paths, store, "pantry")
    by_id = {t["id"]: t for t in data["tickets"]}
    assert [t["id"] for t in data["tickets"]] == ["env-v1", "P-1", "P-2", "P-3", "qa-v1-1", "fix-v1-1",
                                                  "approve-verify-v1"]
    assert by_id["env-v1"]["column"] == "done"
    assert by_id["P-1"]["column"] == "done" and any(e["source"] == "progress" for e in by_id["P-1"]["logs"])
    assert by_id["P-2"]["column"] == "progress" and by_id["P-2"]["state"] == "running"
    assert any("ProductController" in e["text"] for e in by_id["P-2"]["logs"])
    assert [n["text"] for n in by_id["P-2"]["notes"]] == ["use 409 for duplicates"]
    assert by_id["P-2"]["notes"][0]["pinned"] is True
    assert by_id["P-3"]["column"] == "todo"
    assert by_id["qa-v1-1"]["kind"] == "qa" and by_id["qa-v1-1"]["notes"][0]["author"] == "Claude"
    assert by_id["fix-v1-1"]["kind"] == "bugfix"
    assert [c["label"] for c in data["columns"]] == ["To do", "In progress", "Waiting for approval", "Completed",
                                                     "Failed / cancelled"]


async def test_review_issues_become_the_fix_ticket_and_gate_waits_for_owner(sandbox: Path, tmp_path: Path):
    paths = _paths(sandbox, tmp_path)
    project = _project(paths, state="awaiting_verification")
    (Path(project.dir) / ".law" / "PLAN.md").write_text(PLAN.replace("- [ ]", "- [x]"))
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    root = await store.submit(Job(kind="project_step", payload={"project": "pantry", "_stages": _specs()}))
    await store.finish(root, Status.DONE, result={})
    review = {"score": 3, "issues": ["db port 5432 on 0.0.0.0"], "summary": "ok"}
    for i in range(4):
        await _stage(store, root, i, Status.DONE, finished=time.time() - 100 + i,
                     result={"review": review} if i == 2 else {},
                     payload={"_carry": {"review": review}} if i == 3 else {})
    await _stage(store, root, 4, Status.DONE, finished=time.time(), result={"answer": "Is it good?"})

    by_id = {t["id"]: t for t in (await board.build(paths, store, "pantry"))["tickets"]}
    assert "db port 5432" in by_id["fix-v1-1"]["details"]
    assert any(e["source"] == "review" and "Score 3/5" in e["text"] for e in by_id["qa-v1-1"]["logs"])
    gate = by_id["approve-verify-v1"]
    assert gate["column"] == "approval" and gate["details"] == "Is it good?"

    project.log("allgood")
    project.state = "done"
    Projects(paths).save(project)
    gate = {t["id"]: t for t in (await board.build(paths, store, "pantry"))["tickets"]}["approve-verify-v1"]
    assert gate["column"] == "done" and gate["state"] == "All good"


async def test_a_later_run_supersedes_a_cancelled_one(sandbox: Path, tmp_path: Path):
    paths = _paths(sandbox, tmp_path)
    _project(paths)
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    first = await store.submit(Job(kind="project_step", payload={"project": "pantry", "_stages": _specs()}))
    await store.finish(first, Status.CANCELLED, error="cancelled by user")
    await _stage(store, first, 0, Status.CANCELLED)
    second = await store.submit(Job(kind="project_step", payload={"project": "pantry", "_stages": _specs()}))
    await store.mark_waiting(second)
    await _stage(store, second, 0, Status.DONE, finished=time.time())
    by_id = {t["id"]: t for t in (await board.build(paths, store, "pantry"))["tickets"]}
    assert by_id["env-v1"]["column"] == "done"


@pytest.fixture()
def client(sandbox: Path, tmp_path: Path):
    paths = _paths(sandbox, tmp_path)
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    app = create_app(paths, store=store, resolve=lambda j: j)
    with TestClient(app) as c:
        yield c, paths, store


def test_notes_api_add_list_delete_and_to_agent(client):
    c, paths, _ = client
    project = _project(paths)
    assert c.get("/board").json() == [{"id": "pantry", "name": "Pantry", "state": "developing", "version": 1}]
    r = c.post("/board/pantry/notes", json={"text": "  check the PEM path ", "ticket": "P-2", "to_agent": True})
    assert r.status_code == 200 and r.json()["text"] == "check the PEM path"
    assert (Path(project.dir) / ".law" / "NOTES.md").read_text().strip() == "check the PEM path"
    p2 = next(t for t in c.get("/board/pantry").json()["tickets"] if t["id"] == "P-2")
    assert [n["text"] for n in p2["notes"]] == ["check the PEM path"]
    assert c.post("/board/pantry/notes", json={"text": " "}).status_code == 422
    assert c.post("/board/nope/notes", json={"text": "x"}).status_code == 404
    assert c.delete(f"/board/pantry/notes/{r.json()['id']}").status_code == 200
    assert c.delete(f"/board/pantry/notes/{r.json()['id']}").status_code == 404
    assert c.get("/board/nope").status_code == 404


def test_delete_project_removes_jobs_notes_and_record(client):
    c, paths, store = client
    project = _project(paths)
    board.add_note(paths, "pantry", "hello")
    root = c.portal.call(store.submit, Job(kind="project_step", payload={"project": "pantry", "_stages": _specs()}))
    c.portal.call(store.event, root.id, "progress", {"message": "x"})
    r = c.delete("/board/pantry")
    assert r.status_code == 409 and "cancel it first" in r.json()["detail"]  # a job is running

    c.portal.call(store.request_cancel, root.id)
    r = c.delete("/board/pantry?code=true")
    assert r.status_code == 200 and r.json()["jobs"] == 1
    assert c.portal.call(store.get, root.id) is None and c.portal.call(store.events, root.id) == []
    assert not Path(project.dir).exists() and Path(r.json()["trashed"]).is_relative_to(paths.trash)
    assert board.notes(paths, "pantry") == [] and c.get("/board").json() == []


async def test_delete_never_trashes_a_folder_outside_the_projects_root(sandbox: Path, tmp_path: Path):
    paths = _paths(sandbox, tmp_path)
    outside = tmp_path / "elsewhere"
    (outside / ".law").mkdir(parents=True)
    Projects(paths).save(Project(id="x", name="X", dir=str(outside), summary="s", details="d"))
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    with pytest.raises(ValueError, match="not a project folder"):
        await board.delete_project(paths, store, "x", code=True)
    assert outside.is_dir() and Projects(paths).get("x").id == "x"
