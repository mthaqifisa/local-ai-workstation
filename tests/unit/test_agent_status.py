"""Agent health for the Console and Telegram /jobs: working, thinking, stalled, stuck, failed."""

from __future__ import annotations

from pathlib import Path

import fakeredis

from law import agent_status as st
from law.queue.jobs import Job, Status
from law.queue.store import JobStore

NOW = 10_000.0


def _job(status: Status, started: float | None = None, stages: bool = False) -> Job:
    return Job(kind="task", payload={"_stages": [{}, {}]} if stages else {}, status=status, started=started)


def test_health_states():
    running = _job(Status.RUNNING, started=NOW - 3000)
    assert st.health(running, None, NOW - 60, NOW - 60, True, NOW) == "working"
    assert st.health(running, None, NOW - 900, NOW - 900, True, NOW) == "thinking"
    assert st.health(running, None, NOW - 900, NOW - 1500, True, NOW) == "thinking"  # generating: grace applies
    assert st.health(running, None, NOW - 900, NOW - 2400, True, NOW) == "stalled"
    assert st.health(running, None, NOW - 900, NOW - 1500, False, NOW) == "stalled"
    assert st.health(running, None, None, None, False, NOW) == "stalled"
    root = _job(Status.WAITING, stages=True)
    assert st.health(root, None, None, None, True, NOW) == "stuck"  # nothing running, nothing failed
    assert st.health(root, _job(Status.QUEUED), None, None, True, NOW) == "queued"
    assert st.health(_job(Status.DEAD), None, None, None, None, NOW) == "failed"
    assert st.actions_for("failed", True) == ["retry", "skip"] and st.actions_for("failed", False) == ["retry"]
    assert st.actions_for("stalled", True) == ["restart", "cancel"] and st.actions_for("working", True) == ["cancel"]


def test_plan_summary_reads_the_board(tmp_path: Path):
    assert st.plan_summary(tmp_path) is None
    (tmp_path / ".law").mkdir()
    (tmp_path / ".law" / "PLAN.md").write_text("## Build version 1\n- [x] LC-1 api\n- [ ] LC-2 ui\n- [ ] LC-3 docs\n")
    (tmp_path / ".law" / "PROGRESS.md").write_text("# Progress\n\n## 10:00\nLC-1 done, tests green\n")
    summary = st.plan_summary(tmp_path)
    assert summary == {"done": 1, "total": 3, "next": "LC-2 ui", "progress": "## 10:00\nLC-1 done, tests green"}


async def test_statuses_report_the_running_stage_and_filter_by_keyword(tmp_path: Path):
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    root = await store.submit(Job(kind="project_new", payload={"fields": {"name": "LAN Chat", "repo": "lan-chat"},
                                                                 "_stages": [{}, {}, {}]}, lane="none"))
    await store.mark_waiting(root)
    stage = await store.submit(Job(kind="task_work", payload={"role": "writer", "dir": str(tmp_path)},
                                   parent=root.id, stage=1, lane="none"))
    assert await store.claim(stage)
    await store.event(stage.id, "progress", {"message": "✏️ writing docs/proposal/proposal.md"})
    await store.submit(Job(kind="image", payload={"prompt": "a lighthouse"}, lane="none"))
    found = await st.statuses(store, {"active": True}, "lan")
    assert len(found) == 1
    s = found[0]
    assert s["title"] == "LAN Chat" and s["health"] == "working" and s["stage"]["index"] == 2
    assert s["stage"]["role"] == "writer" and s["last_10min"]["edits"] == 1 and s["project"] == str(tmp_path)
    assert {x["title"] for x in await st.statuses(store, {"active": True})} == {"LAN Chat", "a lighthouse"}


def test_numbered_checklists_count(tmp_path: Path):
    (tmp_path / ".law").mkdir()
    (tmp_path / ".law" / "PLAN.md").write_text("1. [x] read brief\n2. [ ] search market\n* [x] tick\n")
    summary = st.plan_summary(tmp_path)
    assert summary is not None and (summary["done"], summary["total"], summary["next"]) == (2, 3, "search market")


async def test_stopped_jobs_in_a_folder_with_newer_work_are_hidden(tmp_path: Path):
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    old = await store.submit(Job(kind="task", payload={"prompt": "old run", "dir": str(tmp_path)}, lane="none"))
    await store.finish(old, Status.FAILED, error="Traceback\n  File x\nValueError: boom")
    other = await store.submit(Job(kind="task", payload={"prompt": "elsewhere", "dir": "/other"}, lane="none"))
    await store.finish(other, Status.FAILED, error="bad")
    new = await store.submit(Job(kind="task", payload={"prompt": "new run", "dir": str(tmp_path)}, lane="none"))
    assert await store.claim(new)
    shown = {s["title"]: s for s in await st.statuses(store, {"active": True})}
    assert set(shown) == {"new run", "elsewhere"} and shown["elsewhere"]["error_line"] == "bad"
    assert st.last_line("Traceback\n  File x\nValueError: boom") == "ValueError: boom"
