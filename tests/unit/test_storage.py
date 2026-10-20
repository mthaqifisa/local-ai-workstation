"""Retention pruning (jobs, agent sessions) and cache clearing."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest

from law import storage
from law.paths import LawPaths
from law.queue.jobs import Job, Status
from law.queue.store import JobStore

REPO = Path(__file__).resolve().parents[2]


async def test_prune_keeps_live_jobs_and_the_stages_of_live_pipelines():
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    old = time.time() - 20 * 86400

    async def make(status: Status, parent: str | None = None) -> Job:
        job = await store.submit(Job(kind="k", payload={}, parent=parent, created=old))
        await store.r.zadd("law:history", {job.id: old})
        if status is not Status.QUEUED:
            job.status = status
            await store.save(job)
        return job

    done = await make(Status.DONE)
    running = await make(Status.RUNNING)
    live_root = await make(Status.WAITING)
    stage_of_live = await make(Status.FAILED, parent=live_root.id)
    await store.event(done.id, "progress", {"message": "x"})
    deleted = await store.prune(time.time() - 14 * 86400)
    assert deleted == 1 and await store.get(done.id) is None and await store.events(done.id) == []
    assert all([await store.get(j.id) for j in (running, live_root, stage_of_live)])
    assert await store.prune(time.time() - 30 * 86400) == 0  # nothing that old


def test_clear_empties_caches_but_keeps_recent_temp_files(tmp_path: Path):
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    (paths.cache / "m2" / "org").mkdir(parents=True)
    (paths.cache / "m2" / "org" / "x.jar").write_bytes(b"x" * 10_000)
    paths.tmp.mkdir(parents=True, exist_ok=True)
    fresh, stale = paths.tmp / "fresh.txt", paths.tmp / "stale.txt"
    fresh.write_text("now")
    stale.write_text("old")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    out = storage.clear(paths, ["maven", "tmp"], lambda: None, busy=False)
    assert (paths.cache / "m2").is_dir() and not any((paths.cache / "m2").iterdir())
    assert fresh.exists() and not stale.exists() and out["freed_bytes"] > 0
    with pytest.raises(ValueError, match="unknown cache"):
        storage.clear(paths, ["nope"], lambda: None, busy=False)


def test_prune_sessions_deletes_only_old_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    paths.home.mkdir(parents=True)
    now = time.time()
    sessions = [{"id": "ses_old", "updated": (now - 20 * 86400) * 1000},
                {"id": "ses_new", "updated": (now - 86400) * 1000}]
    deleted: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        assert kw["env"]["XDG_DATA_HOME"].endswith("LAW/data/opencode/data")
        if cmd[:3] == ["opencode", "session", "list"]:
            return SimpleNamespace(stdout=json.dumps(sessions), returncode=0)
        deleted.append(cmd[-1])
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert storage.prune_sessions(paths, 14, now) == 1 and deleted == ["ses_old"]
