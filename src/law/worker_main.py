"""Worker process entry point: load the registry, connect to Valkey, run the queue worker."""

from __future__ import annotations

import asyncio
import logging
import signal

from law.api import redis_url
from law.app import capacity, handlers, resolver
from law.handlers.base import Runtime
from law.llm import OmlxModelControl
from law.paths import LawPaths
from law.queue.store import JobStore
from law.queue.worker import Worker

log = logging.getLogger("law.worker")


def build_worker(paths: LawPaths) -> tuple[Worker, JobStore]:
    rt = Runtime.load(paths)
    store = JobStore.connect(redis_url(rt.reg))
    light = {m.id for m in rt.reg.llm_models if m.lane == "light"}
    internal = f"http://{rt.reg.network.loopback}:{rt.reg.ports.llm_internal}"
    models = OmlxModelControl(internal, light)
    from law.install.steps import WORKER_RESTART_FLAG
    from law.queue.awake import KeepAwake, caffeinate_argv

    worker = Worker(store, handlers(rt), capacity(rt), resolver(rt), models=models,
                    restart_flag=paths.state / WORKER_RESTART_FLAG, awake=KeepAwake(caffeinate_argv()))
    return worker, store


PRUNE_EVERY_S = 6 * 3600


async def prune_once(paths: LawPaths, store: JobStore) -> tuple[int, int]:
    """Apply the retention settings (read fresh, so Console edits take effect): old finished jobs and old agent
    conversations. Returns (jobs, sessions) deleted."""
    import time

    from law import registry, storage

    keep = registry.load(paths.registry_file).retention
    jobs = await store.prune(time.time() - keep.jobs_days * 86400)
    sessions = await asyncio.to_thread(storage.prune_sessions, paths, keep.agent_sessions_days)
    return jobs, sessions


async def _prune_forever(paths: LawPaths, store: JobStore) -> None:
    while True:
        try:
            jobs, sessions = await prune_once(paths, store)
            logging.getLogger("law.worker").info("retention: pruned %s jobs, %s agent sessions", jobs, sessions)
        except Exception:  # never let housekeeping take the worker down
            logging.getLogger("law.worker").exception("retention prune failed")
        await asyncio.sleep(PRUNE_EVERY_S)


NIGHTLY_QA_HOUR = 3  # local time: finished projects are QA'd while nobody uses the Mac (ADR-0018)
LIVE_STATES = {"queued", "running", "waiting"}


def seconds_until(hour: int, now: float | None = None) -> float:
    """Seconds from `now` to the next `hour`:00 local time."""
    import time
    from datetime import datetime, timedelta

    current = datetime.fromtimestamp(now if now is not None else time.time())
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


async def nightly_qa_once(paths: LawPaths, store: JobStore) -> list[str]:
    """QA every finished project (done, or awaiting the owner's verification) that has nothing running. The run
    reports to Telegram only when it finds a blocker or major bug. Returns the projects started."""
    from law import projects
    from law.queue.jobs import Job

    busy = {j.payload.get("project") for j in await store.history(500)
            if j.parent is None and j.status.value in LIVE_STATES}
    resolve = resolver(Runtime.load(paths))
    started = []
    for project in projects.Projects(paths).all():
        if project.state in {"done", "awaiting_verification"} and project.id not in busy:
            await store.submit(resolve(Job(kind="project_qa", payload={"project": project.id, "nightly": True},
                                           source="nightly")))
            started.append(project.id)
    return started


async def _nightly_qa_forever(paths: LawPaths, store: JobStore) -> None:
    while True:
        await asyncio.sleep(seconds_until(NIGHTLY_QA_HOUR))
        try:
            log.info("nightly QA: %s", ", ".join(await nightly_qa_once(paths, store)) or "nothing to test")
        except Exception:  # never let housekeeping take the worker down
            log.exception("nightly QA failed to start")


async def _run(paths: LawPaths) -> None:
    worker, store = build_worker(paths)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    pruner = asyncio.create_task(_prune_forever(paths, store))
    nightly = asyncio.create_task(_nightly_qa_forever(paths, store))
    try:
        await worker.run()
    finally:
        pruner.cancel()
        nightly.cancel()
        await store.close()


def main(paths: LawPaths | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_run(paths or LawPaths.from_env()))


if __name__ == "__main__":
    main()
