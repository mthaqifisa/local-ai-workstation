"""Caches and history that grow on this Mac: their sizes, clearing them, and pruning by the retention settings.

Used by the Console (Storage tab), `law reclaim` and the worker's 6-hourly prune. Every item says what clearing it
costs (usually a re-download or a slower first answer). Items outside ~/LAW (Homebrew, the user's own npm cache)
are labelled as such; browser caches are never touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from law.paths import LawPaths

if TYPE_CHECKING:
    from law.queue.store import JobStore

OTHER = ("pip", "puppeteer", "opencode", "playwright", "torch")


@dataclass
class CacheItem:
    id: str
    label: str
    effect: str  # what clearing it costs
    paths: list[Path]
    clear: Callable[[], None]
    outside_law: bool = False  # created by tools the owner may also use themselves
    needs_idle: bool = False  # only while no job is running (a job may be using it)
    extra: dict[str, Any] = field(default_factory=dict)


def size_bytes(paths: list[Path]) -> int:
    existing = [str(p) for p in paths if p.exists()]
    if not existing:
        return 0
    out = subprocess.run(["du", "-sk", *existing], capture_output=True, text=True, timeout=120)
    return sum(int(line.split()[0]) * 1024 for line in out.stdout.splitlines() if line.split())


def _empty(folder: Path) -> None:
    """Remove a folder's contents but keep the folder (tools expect it to exist)."""
    if not folder.is_dir():
        return
    for item in folder.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def _run(cmd: list[str], env: dict[str, str] | None = None, timeout: float = 1200) -> None:
    import os

    subprocess.run(cmd, env={**os.environ, **(env or {})}, capture_output=True, timeout=timeout, check=False)


def items(paths: LawPaths, restart_llm: Callable[[], None]) -> list[CacheItem]:
    cache = paths.cache
    omlx_cache = paths.runtime / "omlx" / "cache"
    sock = paths.home / "colima" / "default" / "docker.sock"

    def clear_omlx() -> None:
        _empty(omlx_cache)
        restart_llm()  # the server's in-memory index points at the deleted files

    def clearer(*folders: str) -> Callable[[], None]:
        def run() -> None:
            for f in folders:
                _empty(cache / f)
        return run

    def docker_prune() -> None:
        if sock.exists():
            _run(["docker", "system", "prune", "-f"], {"DOCKER_HOST": f"unix://{sock}"})

    def clear_tmp() -> None:
        now = time.time()
        for item in paths.tmp.iterdir() if paths.tmp.is_dir() else []:
            if now - item.stat().st_mtime <= 3600:  # never files a running job just made
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)

    return [
        CacheItem("llm_prompt_cache", "Model prompt cache (oMLX)", "slower first answers until it refills; "
                  "the model restarts, so only when nothing is running", [omlx_cache], clear_omlx, needs_idle=True),
        CacheItem("uv", "Python packages (uv)", "re-downloaded on the next install or agent build",
                  [cache / "uv"], lambda: _run(["uv", "cache", "clean"], {"UV_CACHE_DIR": str(cache / "uv")}),
                  needs_idle=True),
        CacheItem("npm", "JavaScript packages (npm, LAW's)", "re-downloaded on the next agent build",
                  [cache / "npm"], clearer("npm"), needs_idle=True),
        CacheItem("maven", "Java packages (Maven, Gradle)", "re-downloaded on the next Java build",
                  [cache / "m2", cache / "gradle"], clearer("m2", "gradle"), needs_idle=True),
        CacheItem("pip", "Other tool caches (pip, puppeteer, opencode)", "re-downloaded when needed",
                  [cache / n for n in OTHER], clearer(*OTHER), needs_idle=True),
        CacheItem("tmp", "Temporary files", "none (files older than an hour)", [paths.tmp], clear_tmp),
        CacheItem("docker", "Docker: unused images and build cache", "rebuilt or re-pulled if needed again",
                  [paths.home / "colima"], docker_prune,
                  extra={"note": "size is the whole Docker disk; pruning frees part of it"}),
        CacheItem("homebrew", "Homebrew downloads", "none; old installers are re-downloaded only if needed",
                  [paths.user_home / "Library" / "Caches" / "Homebrew"], lambda: _run(["brew", "cleanup", "-s"]),
                  outside_law=True),
        CacheItem("npm_user", "Your own npm cache (~/.npm)", "your npm re-downloads packages; not created by LAW",
                  [paths.user_home / ".npm"], lambda: _run(["npm", "cache", "clean", "--force"]), outside_law=True),
    ]


def report(paths: LawPaths, restart_llm: Callable[[], None]) -> list[dict[str, Any]]:
    return [{"id": i.id, "label": i.label, "effect": i.effect, "bytes": size_bytes(i.paths),
             "outside_law": i.outside_law, "needs_idle": i.needs_idle, **i.extra} for i in items(paths, restart_llm)]


def clear(paths: LawPaths, ids: list[str], restart_llm: Callable[[], None], busy: bool) -> dict[str, Any]:
    """Clear the chosen caches. Items that need an idle Mac are skipped (and reported) while jobs run."""
    known = {i.id: i for i in items(paths, restart_llm)}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"unknown cache: {', '.join(unknown)}")
    freed, skipped = 0, []
    for cid in ids:
        item = known[cid]
        if item.needs_idle and busy:
            skipped.append(cid)
            continue
        before = size_bytes(item.paths)
        item.clear()
        freed += max(0, before - size_bytes(item.paths))
    return {"freed_bytes": freed, "skipped_busy": skipped}


def prune_sessions(paths: LawPaths, days: int, now: float | None = None) -> int:
    """Delete agents' OpenCode sessions not updated for `days`. Returns how many."""
    import os

    base = paths.data / "opencode"  # the same dirs the agents run with (law.handlers.tasks.agent_env)
    full = {**os.environ, "XDG_DATA_HOME": str(base / "data"), "XDG_STATE_HOME": str(base / "state"),
            "XDG_CACHE_HOME": str(paths.cache / "opencode")}
    cutoff = ((now or time.time()) - days * 86400) * 1000
    out = subprocess.run(["opencode", "session", "list", "--format", "json"], capture_output=True, text=True,
                         timeout=120, env=full, cwd=paths.home)
    try:
        sessions = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return 0
    old = [s["id"] for s in sessions if float(s.get("updated") or s.get("created") or 0) < cutoff]
    for sid in old:
        subprocess.run(["opencode", "session", "delete", sid], capture_output=True, timeout=60, env=full,
                       cwd=paths.home)
    return len(old)


FINISHED_PROJECT = {"done", "parked"}


async def hard_reset(paths: LawPaths, store: JobStore, restart_llm: Callable[[], None],
                     wait_s: float = 120.0) -> dict[str, Any]:
    """Tony's /hardreset: stop every job, then delete all job history, agent conversations, Tony's chat memory and
    every LAW cache. Kept: models, settings, secrets, outputs, projects and their .law/ memory, and the jobs of
    unfinished projects (their Retry continues the build). Returns what was done."""
    import asyncio

    from law.projects import Projects

    live = [j for j in await store.history(limit=100_000) if j.parent is None and not j.status.terminal]
    for job in live:
        await store.request_cancel(job.id)
    for _ in range(int(wait_s)):  # the worker (another process) stops each one; poll the queue until it has
        if not await store.running_ids():
            break
        await asyncio.sleep(1)
    busy = bool(await store.queued() or await store.running_ids())

    active = {p.id for p in Projects(paths).all() if p.state not in FINISHED_PROJECT}
    keep = frozenset(j.id for j in await store.history(limit=100_000)
                     if j.parent is None and j.payload.get("project") in active)
    jobs = await store.prune(time.time() + 1, keep)
    sessions = await asyncio.to_thread(prune_sessions, paths, 0)

    chats = paths.state / "telegram-chats"
    cleared_chats = len(list(chats.glob("*.json"))) if chats.is_dir() else 0
    _empty(chats)
    ids = [i.id for i in items(paths, restart_llm) if not i.outside_law]
    caches = await asyncio.to_thread(clear, paths, ids, restart_llm, busy)
    return {"cancelled": len(live), "jobs_deleted": jobs, "project_jobs_kept": len(keep),
            "agent_sessions_deleted": sessions, "chats_cleared": cleared_chats, **caches}
