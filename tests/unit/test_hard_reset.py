"""/hardreset: every job stopped and history, conversations, chat memory and LAW caches cleared; models, projects
and the jobs an unfinished project's Retry needs are kept."""

from __future__ import annotations

from pathlib import Path

import fakeredis
import pytest

from law import projects, storage
from law.paths import LawPaths
from law.queue.jobs import Job, Status
from law.queue.store import JobStore
from law.telegram_bot import reset_summary

REPO = Path(__file__).resolve().parents[2]
FIELDS = {"name": "N", "thinking": "low", "summary": "s", "details": "d"}


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    cleared: list[str] = []
    sessions: list[int] = []

    def fake_items(p, restart):  # type: ignore[no-untyped-def]
        def item(cid: str, outside: bool = False) -> storage.CacheItem:
            return storage.CacheItem(cid, cid, "x", [], lambda: cleared.append(cid), outside_law=outside,
                                     needs_idle=cid == "uv")
        return [item("llm_prompt_cache"), item("uv"), item("tmp"), item("homebrew", outside=True)]

    monkeypatch.setattr(storage, "items", fake_items)
    monkeypatch.setattr(storage, "prune_sessions", lambda p, days: sessions.append(days) or 3)
    yield paths, store, cleared, sessions
    await store.close()


def project(paths: LawPaths, pid: str, state: str) -> None:
    reg = projects.Projects(paths)
    p = reg.create({**FIELDS, "repo": pid}, chat=1)
    p.state = state  # type: ignore[assignment]
    reg.save(p)


async def test_hard_reset_clears_everything_but_unfinished_projects(env):
    paths, store, cleared, sessions = env
    project(paths, "building", "developing")
    project(paths, "shipped", "done")
    done = await store.submit(Job(kind="ask", payload={"prompt": "hi"}))
    await store.finish(done, Status.DONE, result={"answer": "hello"})
    live = await store.submit(Job(kind="ask", payload={"prompt": "still waiting"}))
    build = await store.submit(Job(kind="project_step", payload={"project": "building"}))
    await store.finish(build, Status.CANCELLED, error="cancelled by user")
    stage = await store.submit(Job(kind="task_work", payload={}, parent=build.id))
    await store.finish(stage, Status.CANCELLED, error="cancelled by user")
    old = await store.submit(Job(kind="project_step", payload={"project": "shipped"}))
    await store.finish(old, Status.DONE, result={})
    chats = paths.state / "telegram-chats"
    chats.mkdir(parents=True)
    (chats / "1.json").write_text("[]")

    out = await storage.hard_reset(paths, store, lambda: None, wait_s=1)

    assert out["cancelled"] == 1 and (await store.get(live.id)) is None  # stopped, then deleted
    assert (await store.get(done.id)) is None and (await store.get(old.id)) is None
    assert (await store.get(build.id)) is not None and (await store.get(stage.id)) is not None
    assert out["project_jobs_kept"] == 1 and out["jobs_deleted"] == 3
    assert sessions == [0] and out["agent_sessions_deleted"] == 3
    assert out["chats_cleared"] == 1 and not list(chats.iterdir())
    assert cleared == ["llm_prompt_cache", "uv", "tmp"]  # never the owner's own (outside ~/LAW) caches
    assert (paths.state / "projects.json").exists()


async def test_caches_a_still_running_job_may_use_are_left_for_later(env):
    paths, store, cleared, _ = env
    running = await store.submit(Job(kind="ask", payload={}))
    await store.claim(running)  # the worker would stop it; here nothing does

    out = await storage.hard_reset(paths, store, lambda: None, wait_s=1)

    assert out["skipped_busy"] == ["uv"] and "uv" not in cleared and "tmp" in cleared
    assert "run /hardreset again" in reset_summary(out)


def test_summary_reports_what_was_done():
    text = reset_summary({"cancelled": 2, "jobs_deleted": 40, "agent_sessions_deleted": 5, "chats_cleared": 1,
                          "freed_bytes": 3 * 1024 ** 3, "skipped_busy": [], "project_jobs_kept": 4})
    assert "2 job(s) stopped, 40 deleted" in text and "3.0 GB freed" in text and "kept 4 job(s)" in text
